import json, glob
import numpy as np

FILES = sorted(glob.glob("/Users/Denis/Dev/Deeptrading/backend/reports/_h084_085_*.json"))
data = [json.load(open(f)) for f in FILES]
syms = [d["vpin"]["figi"] for d in data]
print("tickers", syms)

# H-084 VPIN aggregation
vp = [d["vpin"] for d in data]
full = np.array([v["full_ret"] for v in vp]); filt = np.array([v["filt_ret"] for v in vp])
cov = np.array([v["coverage"] for v in vp]); n = np.array([v["n"] for v in vp])
ratio_pool = float((filt*n).sum()/(full*n).sum()) if (full*n).sum()!=0 else None
cov_pool = float((cov*n).sum()/n.sum())
h084 = dict(per_ticker={v["figi"]: {"full_ret":round(v["full_ret"],4),"filt_ret":round(v["filt_ret"],4),
                                     "coverage":round(v["coverage"],3),"ratio":round(v["ratio"],3),
                                     "gate":v["gate_passed"]} for v in vp},
            pooled_ratio=round(ratio_pool,3) if ratio_pool else None,
            pooled_coverage=round(cov_pool,3),
            gate_passed=bool(ratio_pool is not None and ratio_pool>=1.15 and cov_pool>=0.5),
            note="VPIN top-decile filter removes ~10% toxic-flow trades; no net/trade improvement")
json.dump(h084, open("/Users/Denis/Dev/Deeptrading/backend/reports/h084_microstructure.json","w"), indent=2)

# H-085 Factor IC aggregation
sigs = list(data[0]["ic"].keys())
ic_mean = {}; icir_mean = {}; sig_detail = {}
for s in sigs:
    icv = np.array([d["ic"][s] for d in data]); icirv = np.array([d["icir"][s] for d in data])
    ic_mean[s] = round(float(icv.mean()),3); icir_mean[s] = round(float(icirv.mean()),2)
    sig_detail[s] = dict(ic_per_ticker={syms[i]: round(float(data[i]["ic"][s]),3) for i in range(len(syms))},
                         icir_per_ticker={syms[i]: round(float(data[i]["icir"][s]),2) for i in range(len(syms))},
                         significant=bool(abs(icv.mean())>0.02 and abs(icirv.mean())>2))
h085 = dict(n_total=int(np.array([d["n_total"] for d in data]).sum()),
            signals=sigs,
            ic_mean=ic_mean, icir_mean=icir_mean,
            significant=[s for s in sigs if sig_detail[s]["significant"]],
            detail=sig_detail,
            note="Only ATR/NATR show significant IC (t>>2). rsi/macd/roc/cci/vortex NOT significant as standalone IC.")
json.dump(h085, open("/Users/Denis/Dev/Deeptrading/backend/reports/h085_factor_ic.json","w"), indent=2)
print(json.dumps({"h084":h084,"h085":h085}, indent=2, ensure_ascii=False))
