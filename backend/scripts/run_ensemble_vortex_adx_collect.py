"""H-077 v3 ЧАСТЬ 2 collect: читает reports/_ens_partial_{sym}.json (5 figi),
собирает S0/S1b/S2, считает метрики, пишет reports/ensemble_vortex_adx.json."""
import json, math, os

FIGIS = {
    "BBG008F2T3T2": "RUAL", "BBG004S681M2": "SNGP", "BBG004S683W7": "AFLT",
    "BBG004S68CP5": "MVID", "BBG004S681B4": "NLMK",
}

def metrics(trades, label):
    if not trades:
        return dict(label=label, n=0)
    netv = [float(t["net"]) for t in trades]
    net = sum(netv)
    wins = [x for x in netv if x > 0]
    losses = [x for x in netv if x <= 0]
    win_rate = len(wins) / len(netv)
    g = sum(math.log(1 + x / 10000) for x in netv)
    denom = sum(losses)
    pf = (sum(wins) / abs(denom)) if denom < 0 else None
    peak = 0; dd = 0; run = 0
    for x in netv:
        run += x; peak = max(peak, run); dd = min(dd, run - peak)
    return dict(label=label, n=len(netv), net=round(net, 1),
                avg_per_trade=round(net / len(netv), 2),
                win_rate=round(win_rate, 3),
                profit_factor=round(pf, 2) if pf is not None else None,
                max_dd=round(dd, 1), growth=round(math.exp(g), 3))

def main():
    S0 = []; S1b = []; S2 = []
    for figi, sym in FIGIS.items():
        p = f"reports/_ens_partial_{sym}.json"
        if not os.path.exists(p):
            print(f"  MISSING {p}")
            continue
        d = json.load(open(p))
        S0 += d["S0"]; S1b += d["S1b"]; S2 += d["S2"]
        print(f"  {sym}: S0={len(d['S0'])} S1b={len(d['S1b'])} S2={len(d['S2'])}")
    out = dict(
        window="OOS 2025-01..2026-01 (monthly-sharded per figi)",
        config_hash_note="canonical 7/quorum2 unchanged (same config_hash as HARD CONSTRAINT)",
        vortex_note="at quorum=2 adding Vortex as 8th function does NOT change quorum (7 already pass) -> S1(7+Vortex)==S0 mathematically",
        S0=metrics(S0, "S0 canonical (7/quorum2)"),
        S1=metrics(S0, "S1 = S0 (Vortex added, quorum2 unchanged)"),
        S1b=metrics(S1b, "S1b Vortex-confirmed (require-filter, ten"),
        S2=metrics(S2, "S2 = S1b + ADX14>25"),
        criteria=dict(
            C1_ensemble_add_Vortex="Vortex as 8th function: NO change at quorum2 (S1==S0); value only via require-filter (S1b/S2)",
            C2_ADX_trend_filter="S2 vs S0: see net/win_rate/PF below",
        ),
    )
    with open("reports/ensemble_vortex_adx.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print("WROTE reports/ensemble_vortex_adx.json")
    print(json.dumps({k: out[k] for k in ["S0", "S1", "S1b", "S2"]}, indent=2, default=str))

if __name__ == "__main__":
    main()
