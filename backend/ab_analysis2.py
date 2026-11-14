# -*- coding: utf-8 -*-
"""A/B: контроль(tg_pool20 старый / tg_d1base чистый) vs treatment tg_d1filt."""
import pandas as pd, psycopg2

DSN = "host=127.0.0.1 port=5432 dbname=deeptrading user=deeptrading"
NAMES = ["tg_pool20", "tg_d1base", "tg_d1filt"]
conn = psycopg2.connect(DSN)

q = ("SELECT test_name, side, entry_time, exit_time, net_pnl, exit_reason, trailing_active "
     "FROM sandbox_trades WHERE test_name = ANY(%s) ORDER BY test_name, entry_time")
df = pd.read_sql(q, conn, params=(NAMES,))
if df.empty:
    print("no rows")
    raise SystemExit
df["net_pnl"] = df["net_pnl"].astype(float)
df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True)
df["ym"] = df["entry_time"].dt.strftime("%Y-%m")
df["half"] = df["ym"].apply(lambda x: "IS<=2026-06" if x <= "2026-06" else "OOS>2026-06")
df["day"] = df["entry_time"].dt.date


def stats(g):
    if len(g) == 0:
        return dict(n=0, total=0.0, mean=0.0, med=0.0, win=0.0, pf=0.0, avg_w=0.0, avg_l=0.0)
    wins = g[g.net_pnl > 0].net_pnl
    losses = g[g.net_pnl < 0].net_pnl
    pf = (wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else 999.0
    return dict(n=len(g), total=round(g.net_pnl.sum(), 2), mean=round(g.net_pnl.mean(), 3),
                med=round(g.net_pnl.median(), 3), win=round(float((g.net_pnl > 0).mean()), 3),
                pf=round(float(pf), 2), avg_w=round(float(wins.mean()), 3) if len(wins) else 0.0,
                avg_l=round(float(losses.mean()), 3) if len(losses) else 0.0)


def line(tag, g):
    s = stats(g)
    return (f"{tag:28s} n={s['n']:5d} total={s['total']:9.2f} mean={s['mean']:7.3f} med={s['med']:7.3f} "
            f"win={s['win']:.3f} PF={s['pf']:6.2f} avgW={s['avg_w']:7.2f} avgL={s['avg_l']:7.2f}")


for tn in [n for n in NAMES if n in set(df.test_name)]:
    print(f"\n########## {tn} ##########")
    for side in ("BUY", "SELL"):
        print(line(f"  {side} ALL", df[(df.test_name == tn) & (df.side == side)]))
    for side in ("BUY", "SELL"):
        for half in ("IS<=2026-06", "OOS>2026-06"):
            print(line(f"  {side} {half}", df[(df.test_name == tn) & (df.side == side) & (df.half == half)]))
    g = df[df.test_name == tn]
    print(f"  TOTAL n={len(g)} total={g.net_pnl.sum():.2f} avg_trades_per_day={len(g)/max(1,g.day.nunique()):.2f}")
    print(f"  trades/day BUY={len(g[g.side=='BUY'])/max(1,g.day.nunique()):.2f} "
          f"SELL={len(g[g.side=='SELL'])/max(1,g.day.nunique()):.2f}")
    print(f"  exit_reasons={dict(g.exit_reason.fillna('-').value_counts().head(6))}")

print("\n########## monthly total (BUY | SELL) ##########")
for tn in [n for n in NAMES if n in set(df.test_name)]:
    g = df[df.test_name == tn]
    piv = g.pivot_table(index="ym", columns="side", values="net_pnl", aggfunc="sum").fillna(0.0).round(1)
    piv["TOTAL"] = piv.sum(axis=1).round(1)
    print(f"\n--- {tn} ---")
    print(piv.to_string())

print("\n########## filter events (by reason kind) ##########")
ev = pd.read_sql("SELECT occurred_at, payload FROM event_log WHERE event_type='ENTRY_FILTER_D1' ORDER BY id", conn)
pl = ev.payload.apply(lambda p: p if isinstance(p, dict) else {})
dec = pl.apply(lambda d: (d.get("payload") or {}).get("decision"))
rsn = pl.apply(lambda d: d.get("reason") or "")
kind = rsn.apply(lambda r: (r.split("=")[0].replace("+", "") if "=" in r else r))
sel = dec == "allow"
selrsn = rsn[sel].apply(lambda r: r)
print("total:", len(ev), "| decisions:", dict(dec.value_counts()))
for k, v in kind[dec == "reject"].value_counts().items():
    print(f"  reject by {k}: {v}")
print("  allowed:", int(sel.sum()), "| allowed with non-ok reason:", dict(selrsn[selrsn != "ok"].value_counts()))
