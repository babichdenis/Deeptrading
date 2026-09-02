"""ML Training: XGBoost + LightGBM на triple-barrier labeled dataset.
Walk-forward CV, feature importance, DSR, Sharpe оценка.

Использование:
  python3 h_train_ml.py [--data labeled_8m.parquet] [--n-folds 5]
"""
import os, json, argparse
import numpy as np
import polars as pl
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix
from sklearn.preprocessing import StandardScaler
import warnings
warnings.filterwarnings("ignore")

REPORTS = "/Users/Denis/Dev/Deeptrading/backend/reports"


def load_data(path):
    df = pl.read_parquet(path)
    # Убираем NaN в фичах
    feature_cols = [c for c in df.columns if c.startswith(("ret_", "atr", "natr", "rsi", "bb_", "vol_z", "hour", "weekday"))]
    df = df.drop_nulls(subset=feature_cols + ["label"])
    # Бинарная классификация: LONG(+1) vs SHORT(-1), без TIMEOUT(0)
    df_binary = df.filter(pl.col("label").is_in([-1, 1]))
    df_binary = df_binary.with_columns([
        pl.when(pl.col("label") == 1).then(1).otherwise(0).alias("target")
    ])
    # Заменяем inf на NaN и убираем их
    for col in feature_cols:
        df_binary = df_binary.with_columns([
            pl.when(pl.col(col).is_infinite()).then(None).otherwise(pl.col(col)).alias(col)
        ])
    df_binary = df_binary.drop_nulls(subset=feature_cols)
    return df, df_binary, feature_cols


def walk_forward_split(ts_series, n_folds=5, min_train_pct=0.5):
    """Walk-forward: train на прошлом, test на будущем."""
    # Извлекаем даты как строки YYYY-MM-DD
    dates_str = ts_series.dt.strftime("%Y-%m-%d")
    unique_dates = sorted(dates_str.unique().to_list())
    n = len(unique_dates)
    min_train = int(n * min_train_pct)
    fold_size = (n - min_train) // n_folds
    
    folds = []
    for i in range(n_folds):
        train_end = min_train + i * fold_size
        test_start = train_end
        test_end = min(test_start + fold_size, n)
        if test_start >= n or test_end <= test_start:
            break
        train_dates = unique_dates[:train_end]
        test_dates = unique_dates[test_start:test_end]
        folds.append((train_dates, test_dates))
    return folds


def train_xgboost(X_train, y_train, X_test, y_test, feature_cols):
    try:
        import xgboost as xgb
    except ImportError:
        print("XGBoost not installed, skipping...")
        return None
    
    model = xgb.XGBClassifier(
        n_estimators=200, max_depth=6, learning_rate=0.1,
        subsample=0.8, colsample_bytree=0.8,
        scale_pos_weight=2.5,  # баланс LONG/SHORT
        random_state=42, eval_metric="logloss", verbosity=0
    )
    model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)
    
    y_pred = model.predict(X_test)
    y_prob = model.predict_proba(X_test)[:, 1]
    
    acc = accuracy_score(y_test, y_pred)
    importance = dict(zip(feature_cols, model.feature_importances_))
    
    return {"model": "XGBoost", "accuracy": acc, "y_pred": y_pred, "y_prob": y_prob,
            "importance": importance, "clf": model}


def train_lightgbm(X_train, y_train, X_test, y_test, feature_cols):
    try:
        import lightgbm as lgb
    except ImportError:
        print("LightGBM not installed, skipping...")
        return None
    
    model = lgb.LGBMClassifier(
        n_estimators=200, max_depth=6, learning_rate=0.1,
        subsample=0.8, colsample_bytree=0.8,
        class_weight={0: 1.0, 1: 2.5},  # баланс LONG/SHORT
        random_state=42, verbose=-1
    )
    model.fit(X_train, y_train, eval_set=[(X_test, y_test)])
    
    y_pred = model.predict(X_test)
    y_prob = model.predict_proba(X_test)[:, 1]
    
    acc = accuracy_score(y_test, y_pred)
    importance = dict(zip(feature_cols, model.feature_importances_))
    
    return {"model": "LightGBM", "accuracy": acc, "y_pred": y_pred, "y_prob": y_prob,
            "importance": importance, "clf": model}


def compute_sharpe(returns, periods_per_year=252*7.5):
    """Годовой Sharpe из收益率.returns (8m bars → ~7.5 trades/day)."""
    if len(returns) == 0 or returns.std() == 0:
        return 0.0
    return float(returns.mean() / returns.std() * np.sqrt(periods_per_year))


def backtest_signals(df_test, y_pred, y_prob=None, capital=10000, cost_bps=10):
    """Бэктест: long-only, short-only, long-short. ret_1[i] — прошлый, будущий = shift."""
    returns = df_test["ret_1"].to_numpy()
    future_returns = np.roll(returns, -1)
    future_returns[-1] = 0
    
    all_bt = {}
    
    def run_one(signals, label):
        pnl = signals * future_returns
        changes = np.abs(np.diff(signals, prepend=0))
        pnl -= changes * cost_bps / 10000
        pnl = pnl[1:]
        equity = capital * np.cumprod(1 + pnl)
        n_pos = int((signals != 0).sum())
        return {
            "total_return_pct": round(float((equity[-1] / capital - 1) * 100), 2),
            "n_trades": int(changes.sum()),
            "win_rate_pct": round(float((pnl > 0).mean() * 100), 1),
            "sharpe": round(float(compute_sharpe(pl.Series(pnl))), 2),
            "max_dd_pct": round(float(np.min(equity / np.maximum.accumulate(equity) - 1) * 100), 2),
            "final_equity": round(float(equity[-1]), 2),
            "position_pct": round(n_pos / len(signals) * 100, 1),
        }
    
    if y_prob is not None:
        all_bt["long_only"] = run_one(np.where(y_prob > 0.55, 1.0, 0.0), "long")
        all_bt["short_only"] = run_one(np.where(y_prob < 0.45, -1.0, 0.0), "short")
        conf = np.abs(y_prob - 0.5) * 2
        direction = np.where(y_prob > 0.55, 1, np.where(y_prob < 0.45, -1, 0))
        all_bt["long_short"] = run_one(direction * conf, "ls")
    
    # Вернуть long_only как основной результат
    best = all_bt.get("long_only", {"total_return_pct": 0, "sharpe": 0, "max_dd_pct": 0, "win_rate_pct": 0, "n_trades": 0})
    best["_all"] = all_bt
    return best


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=os.path.join(REPORTS, "labeled_8m.parquet"))
    parser.add_argument("--n-folds", type=int, default=5)
    args = parser.parse_args()
    
    print(f"Loading {args.data}...")
    df_all, df_binary, feature_cols = load_data(args.data)
    print(f"Total: {len(df_all)} rows, Binary (LONG/SHORT): {len(df_binary)} rows")
    print(f"Features: {feature_cols}")
    print(f"Target distribution: {df_binary['target'].value_counts().sort('target')}")
    
    # Walk-forward
    df_binary = df_binary.sort("ts")
    folds = walk_forward_split(df_binary["ts"], n_folds=args.n_folds)
    print(f"\nWalk-forward: {len(folds)} folds")
    
    all_results = {"xgboost": [], "lightgbm": []}
    all_importance = {"xgboost": {f: 0.0 for f in feature_cols},
                      "lightgbm": {f: 0.0 for f in feature_cols}}
    
    for fold_idx, (train_dates, test_dates) in enumerate(folds):
        print(f"  Fold {fold_idx+1}: train_dates={len(train_dates)} test_dates={len(test_dates)}")
        print(f"    First train_date: {train_dates[0] if train_dates else 'EMPTY'}")
        print(f"    First test_date: {test_dates[0] if test_dates else 'EMPTY'}")
        
        train_mask = df_binary["ts"].dt.strftime("%Y-%m-%d").is_in(train_dates)
        test_mask = df_binary["ts"].dt.strftime("%Y-%m-%d").is_in(test_dates)
        
        df_train = df_binary.filter(train_mask)
        df_test = df_binary.filter(test_mask)
        
        print(f"    df_train={len(df_train)} df_test={len(df_test)}")
        
        if len(df_train) == 0 or len(df_test) == 0:
            print(f"    SKIP")
            continue
        
        X_train = df_train.select(feature_cols).to_numpy()
        y_train = df_train["target"].to_numpy()
        X_test = df_test.select(feature_cols).to_numpy()
        y_test = df_test["target"].to_numpy()
        
        # Scale
        scaler = StandardScaler()
        X_train = scaler.fit_transform(X_train)
        X_test = scaler.transform(X_test)
        
        print(f"\nFold {fold_idx+1}: train={len(df_train)} test={len(df_test)}")
        
        for name, train_fn in [("xgboost", train_xgboost), ("lightgbm", train_lightgbm)]:
            result = train_fn(X_train, y_train, X_test, y_test, feature_cols)
            if result is None:
                continue
            
            # Backtest
            bt = backtest_signals(df_test, result["y_pred"], result.get("y_prob"))
            all_bt = bt.pop("_all", {})
            
            fold_result = {
                "fold": fold_idx + 1,
                "accuracy": result["accuracy"],
                **bt
            }
            all_results[name].append(fold_result)
            
            # Accumulate importance
            for f in feature_cols:
                all_importance[name][f] += result["importance"][f]
            
            # Print all strategies
            lo = all_bt.get("long_only", {})
            so = all_bt.get("short_only", {})
            ls = all_bt.get("long_short", {})
            print(f"  {name}: acc={result['accuracy']:.3f}")
            print(f"    LONG:  ret={lo.get('total_return_pct',0):+.1f}% sharpe={lo.get('sharpe',0):.2f} "
                  f"DD={lo.get('max_dd_pct',0):.1f}% WR={lo.get('win_rate_pct',0):.0f}% pos={lo.get('position_pct',0):.0f}%")
            print(f"    SHORT: ret={so.get('total_return_pct',0):+.1f}% sharpe={so.get('sharpe',0):.2f} "
                  f"DD={so.get('max_dd_pct',0):.1f}% WR={so.get('win_rate_pct',0):.0f}% pos={so.get('position_pct',0):.0f}%")
            print(f"    LS:    ret={ls.get('total_return_pct',0):+.1f}% sharpe={ls.get('sharpe',0):.2f} "
                  f"DD={ls.get('max_dd_pct',0):.1f}% WR={ls.get('win_rate_pct',0):.0f}%")
    
    # Агрегация
    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    
    summary = {}
    for name in ["xgboost", "lightgbm"]:
        if not all_results[name]:
            continue
        res = all_results[name]
        avg_acc = np.mean([r["accuracy"] for r in res])
        avg_ret = np.mean([r["total_return_pct"] for r in res])
        avg_sharpe = np.mean([r["sharpe"] for r in res])
        avg_dd = np.mean([r["max_dd_pct"] for r in res])
        avg_wr = np.mean([r["win_rate_pct"] for r in res])
        avg_trades = np.mean([r["n_trades"] for r in res])
        
        # Normalize importance
        total_imp = sum(all_importance[name].values())
        if total_imp > 0:
            for f in all_importance[name]:
                all_importance[name][f] /= total_imp
        
        top5 = sorted(all_importance[name].items(), key=lambda x: -x[1])[:5]
        
        summary[name] = {
            "accuracy": round(avg_acc, 3),
            "avg_return_pct": round(avg_ret, 2),
            "avg_sharpe": round(avg_sharpe, 2),
            "avg_max_dd_pct": round(avg_dd, 2),
            "avg_win_rate_pct": round(avg_wr, 1),
            "avg_trades_per_fold": round(avg_trades),
            "top5_features": [(f, round(v, 3)) for f, v in top5],
            "folds": res
        }
        
        print(f"\n{name.upper()}:")
        print(f"  Accuracy:     {avg_acc:.3f}")
        print(f"  Avg Return:   {avg_ret:+.2f}% per fold")
        print(f"  Avg Sharpe:   {avg_sharpe:.2f}")
        print(f"  Avg MaxDD:    {avg_dd:.1f}%")
        print(f"  Avg WinRate:  {avg_wr:.1f}%")
        print(f"  Avg Trades:   {avg_trades:.0f} per fold")
        print(f"  Top5 Features:")
        for f, v in top5:
            print(f"    {f}: {v:.3f}")
    
    # Сохраняем
    out = {
        "data": args.data,
        "n_folds": args.n_folds,
        "feature_cols": feature_cols,
        "summary": summary,
    }
    out_path = os.path.join(REPORTS, "ml_train_results.json")
    with open(out_path, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
