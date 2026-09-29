#!/usr/bin/env python3
"""Сканер роботов OsEngine для docs/osengine/PORTING_MAP.md.

По каждому .cs из ~/OsEngine/project/OsEngine/Robots (кроме Engines,
Test*) собирает: семейство (первый уровень каталога), класс, типы входных
заявок (Buy*/Sell*), типы выходов (CloseAt*/CloseAll*/CancelStopOrders),
есть ли трейлинг, упомянутые индикаторы, маркер спец-движка
(grid/marketmaker/arbitrage/options/rebalancer/...). Печатает сводку и
кладёт полный TSV (robots_registry.tsv) рядом с собой.
"""
from __future__ import annotations

import os
import re
from collections import Counter

ROOT = os.path.expanduser("~/OsEngine/project/OsEngine/Robots")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "robots_registry.tsv")

ENTRY_RE = re.compile(r"\b(BuyAt\w*|SellAt\w*)\s*\(")
EXIT_RE = re.compile(r"\b(CloseAt\w*|CloseAll\w*|CancelStopOrders)\s*\(")
TRAIL_RE = re.compile(r"\bCloseAtTrailing\w*\s*\(")
SPECIAL_RE = re.compile(
    r"(?i)\bgrid\w*|marketmak\w*|arbitrag\w*|spread\w*|hft\w*|option\w*"
    r"|rebalanc\w*|screener\w*|dividend\w*|sector\w*|news\w*|bond\w*|synthetic\w*|index\w*"
)
IND_RE = re.compile(
    r"(?i)\b(sma|ema\d*|atr\w*|rsi\w*|bollinger\w*|envelops?\w*|stochastic\w*|macd\w*"
    r"|adx\w*|ichimoku\w*|cci\w*|momentum\w*|pricechannel\w*|donchian\w*|parabolic\w*"
    r"|zigzag\w*|fractal\w*|alligator\w*|williams\w*|regression\w*|trix\w*"
    r"|vwap\w*|wma\w*|tema\w*|dpo\w*|aroon\w*|supertrend\w*|keltner\w*|awesome\w*"
    r"|obv\w*|mfi\w*|roc\w*|rvi\w*|smi\w*|vsa\w*|pivot\w*|highest\w*|lowest\w*"
    r"|cmo\w*|coppock\w*|fisher\w*|chandelier\w*|stddev\w*)"
)

rows = []
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in ("Engines",)]
    for fn in sorted(filenames):
        if not fn.endswith(".cs") or "test" in fn.lower():
            continue
        path = os.path.join(dirpath, fn)
        rel = os.path.relpath(path, ROOT)
        family = rel.split(os.sep)[0]
        text = open(path, encoding="utf-8", errors="replace").read()
        m = re.search(r"\bclass\s+(\w+)", text)
        cls = m.group(1) if m else fn[:-3]
        entries = Counter(ENTRY_RE.findall(text))
        exits = Counter(EXIT_RE.findall(text))
        trailing = bool(TRAIL_RE.search(text))
        inds = sorted({t.group(1).lower().rstrip("_") for t in IND_RE.finditer(text)})
        special = bool(SPECIAL_RE.search(fn) or SPECIAL_RE.search(rel))
        rows.append({
            "family": family,
            "class": cls,
            "entry": ",".join(f"{k}x{v}" for k, v in sorted(entries.items())),
            "exit": ",".join(f"{k}x{v}" for k, v in sorted(exits.items())),
            "trail": "Y" if trailing else "",
            "ind": ",".join(inds[:8]),
            "special": "SPECIAL" if special else "",
            "rel": rel,
        })

cols = ["family", "class", "entry", "exit", "trail", "ind", "special", "rel"]
with open(OUT, "w", encoding="utf-8") as f:
    f.write("\t".join(cols) + "\n")
    for r in rows:
        f.write("\t".join(str(r[c]) for c in cols) + "\n")

for r in rows:
    print(" | ".join(r[c] for c in ("family", "class", "entry", "exit", "trail", "special")))

print(f"=== TOTAL {len(rows)} ===")
for fam, n in Counter(r["family"] for r in rows).most_common():
    print(f"{fam}: {n}")
print("special:", sum(1 for r in rows if r["special"]))
print("trailing:", sum(1 for r in rows if r["trail"]))
print("with exits:", sum(1 for r in rows if r["exit"]))
print("no-entry:", [r["class"] for r in rows if not r["entry"]][:40])
print("TSV:", OUT)
