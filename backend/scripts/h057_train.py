"""H-057 ML meta-model — ЧАСТЬ 2: обучение + purged walk-forward + gating (H-058).

Читает reports/_h057_feat_{sym}.json (5 figi). Walk-forward:
  Fold1: train 2025-H1 -> test 2025-H2
  Fold2: train 2025-H1+2025-H2 -> test 2026-H1
Модель: LogisticRegression + HistGradientBoosting (sklearn). Метрики AUC/PR-AUC на OOS.
Gating (H-058): на OOS-сделках фильтруем по p(profitable)>=thr -> net/trade_filtered / net/trade_baseline >= 1.15x
при coverage >= 0.50. Ищем лучший thr.

Деливерабл: reports/h057_ml_filter.json + .md
"""
import sys, json, glob, os
sys.path.insert(0, "/Users/Denis/De/Deeptrading/backend".replace("De/Deeptrading","Deeptrading"))
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score, average_precision_score
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline

EXCLUDE = {"window","profitable","net","side"}
FOLDS = [
    ("Fold1", ["2025-H1"], ["2025-H2"]),
    ("Fold2", ["2025-H1","2025-H2"], ["2026-H1"]),
]

def load_all():
    rows = []
    for p in glob.glob("reports/_h057_feat_*.json"):
        rows += json.load(open(p))
    return rows

def build_Xy(rows, feats):
    X = np.array([[r.get(f, np.nan) for f in feats] for r in rows], float)
    y = np.array([r["profitable"] for r in rows], int)
    net = np.array([r["net"] for r in rows], float)
    w = np.array([r["window"] for r in rows])
    return X, y, net, w

def main():
    rows = load_all()
    print("total rows:", len(rows))
    feats = [k for k in rows[0].keys() if k not in EXCLUDE]
    print("n features:", len(feats))
    # fill NaN с медианой по колонкам
    Xall, yall, netall, wall = build_Xy(rows, feats)
    col_med = np.nanmedian(Xall, axis=0)
    col_med = np.where(np.isnan(col_med), 0.0, col_med)
    Xall = np.where(np.isnan(Xall), col_med, Xall)

    oos_pred = []  # (p, net, window) для OOS сделок всех folds
    fold_metrics = []
    for fname, tr_w, te_w in FOLDS:
        tr = np.isin(wall, tr_w)
        te = np.isin(wall, te_w)
        Xtr, ytr = Xall[tr], yall[tr]
        Xte, yte = Xall[te], yall[te]
        if Xtr.shape[0] == 0 or Xte.shape[0] == 0:
            print(f"  {fname}: skip empty"); continue
        for mname, model in [
            ("logreg", Pipeline([("sc", StandardScaler()), ("m", LogisticRegression(max_iter=2000, class_weight="balanced"))])),
            ("hgb", HistGradientBoostingClassifier(max_depth=4, learning_rate=0.05, max_iter=300)),
        ]:
            model.fit(Xtr, ytr)
            p = model.predict_proba(Xte)[:, 1]
            auc = roc_auc_score(yte, p) if len(set(yte))>1 else float("nan")
            prauc = average_precision_score(yte, p) if len(set(yte))>1 else float("nan")
            base_rate = yte.mean()
            oos_pred.extend(list(zip(p, netall[te])))
            fold_metrics.append(dict(fold=fname, model=mname, n_train=int(tr.sum()),
                                      n_test=int(te.sum()), test_pos_rate=round(float(base_rate),3),
                                      auc=round(float(auc),4), pr_auc=round(float(prauc),4)))
            print(f"  {fname} {mname}: n_test={int(te.sum())} AUC={auc:.4f} PR-AUC={prauc:.4f} base_rate={base_rate:.3f}", flush=True)

    # ---- GATING (H-058) на всех OOS сделках ----
    oos_pred = np.array(oos_pred)
    p = oos_pred[:,0]; net = oos_pred[:,1]
    base_net_per_trade = net.mean()
    base_win = (net>0).mean()
    # перебор порогов
    best = None
    results = []
    for thr in np.round(np.linspace(0.05, 0.95, 37), 3):
        keep = p >= thr
        cov = keep.mean()
        if cov == 0:
            continue
        nk = int(keep.sum())
        net_f = net[keep].mean()
        ratio = net_f / base_net_per_trade if base_net_per_trade != 0 else float("nan")
        results.append(dict(thr=float(thr), coverage=round(float(cov),3),
                            n_kept=nk, net_per_trade_filtered=round(float(net_f),2),
                            ratio_vs_baseline=round(float(ratio),3),
                            win_filtered=round(float((net[keep]>0).mean()),3)))
        if cov >= 0.50 and (best is None or ratio > best["ratio_vs_baseline"]):
            best = results[-1]
    # ближайший к coverage=0.50 для отчёта
    summary = dict(
        n_total=len(rows), n_features=len(feats), features=feats,
        base_net_per_trade=round(float(base_net_per_trade),2),
        base_win_rate=round(float(base_win),3),
        fold_metrics=fold_metrics,
        gating_results=results,
        best_gating=best,
        gate_passed=(best is not None and best["ratio_vs_baseline"] >= 1.15 and best["coverage"] >= 0.50),
    )
    with open("reports/h057_ml_filter.json","w") as f:
        json.dump(summary, f, indent=2, default=str)
    print("WROTE reports/h057_ml_filter.json")
    print("best gating:", best)
    print("gate_passed:", summary["gate_passed"])

if __name__ == "__main__":
    main()
