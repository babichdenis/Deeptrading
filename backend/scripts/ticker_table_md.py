"""Markdown-таблица по тикерам из дампа (для отчёта)."""
import json
import sys
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

dump = json.load(open(sys.argv[1], encoding="utf-8"))
turn = {str(k).upper(): float(v or 0) for k, v in (dump.get("turnover") or {}).items()}
agg: dict[str, dict] = {}
for t in sorted(dump["trades"], key=lambda x: x.get("entry") or ""):
    tk = str(t["ticker"]).upper()
    d = agg.setdefault(tk, {"n": 0, "wins": 0, "gw": 0.0, "gl": 0.0, "last5": []})
    pnl = float(t["net"])
    d["n"] += 1
    if pnl > 0:
        d["wins"] += 1
        d["gw"] += pnl
    else:
        d["gl"] += pnl
    if len(d["last5"]) < 5:
        d["last5"].append(1 if pnl > 0 else 0)

print("| Тикер | Trades | Wins | Losses | Gross Win ₽ | Gross Loss ₽ | Net ₽ | PF | WR% | "
      "Avg Win ₽ | Avg Loss ₽ | Оборот ₽/день | WR-5 | Вето |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
rows = []
for tk, d in agg.items():
    net = d["gw"] + d["gl"]
    wr = d["wins"] / d["n"] * 100 if d["n"] else 0
    pf = d["gw"] / abs(d["gl"]) if d["gl"] else 0
    aw = d["gw"] / d["wins"] if d["wins"] else 0
    al = d["gl"] / (d["n"] - d["wins"]) if d["n"] - d["wins"] else 0
    wr5 = sum(d["last5"]) / len(d["last5"]) if d["last5"] else 0
    veto = "bad_history" if (d["n"] >= 20 and net < -50 and wr / 100 < 0.25) else ""
    rows.append((net, tk, d, wr, pf, aw, al, wr5, veto))
for net, tk, d, wr, pf, aw, al, wr5, veto in sorted(rows, reverse=True):
    tv = turn.get(tk, 0)
    tvs = f"{tv/1e6:.1f}M" if tv else "—"
    print(f"| {tk} | {d['n']} | {d['wins']} | {d['n']-d['wins']} | {d['gw']:.1f} | {d['gl']:.1f} | "
          f"{net:.1f} | {pf:.2f} | {wr:.1f} | {aw:.2f} | {al:.2f} | {tvs} | {wr5:.0%} | {veto} |")
