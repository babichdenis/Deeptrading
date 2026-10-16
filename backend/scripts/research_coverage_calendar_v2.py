"""RESEARCH-CORE-V2 (B1): coverage 900 ячеек на календарной оси + реальные доли unknown.

Только чтение сохранённых signals_{a,b,c}. Считает по месяцам: weekday-дни с
m1-данными (по universe-тикерам), долю рассчитанных ячеек, долю unknown
(INCOMPLETE / signals). Для передачи B3.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

BASE = Path("reports/architect_20261011/research_core_v2/b1/20261011T1947Z")
SNAP = BASE / "snapshot"
GROUPS = ("a", "b", "c")


def month_axis(params: dict) -> list[str]:
    s = date.fromisoformat(params["start"][:10])
    e = date.fromisoformat(params["end"][:10])
    out, cur = [], (s.year, s.month)
    while True:
        key = f"{cur[0]:04d}-{cur[1]:02d}"
        out.append(key)
        y, mo = cur
        cur = (y + 1, 1) if mo == 12 else (y, mo + 1)
        if (cur[0], cur[1]) > (e.year, e.month):
            break
    return out


def main() -> None:
    m = json.loads((SNAP / "manifest.json").read_text())
    elig = json.loads((BASE / "eligibility_machine.json").read_text())
    months = month_axis(m["params"])

    # m1-дни по тикеру -> по месяцу (для calendar coverage universe-тикеров)
    cal = {mo: {"m1_tickers": 0, "n_universe": 0} for mo in months}
    for figi, e in m["per_ticker"].items():
        m1 = e["m1"]
        if (m1.get("written_rows") or 0) == 0:
            continue
        # approximation: days_with_data -> месяцы min..max
        mn, mx = m1.get("min_ts_utc"), m1.get("max_ts_utc")
        if not (mn and mx):
            continue
        d0 = date.fromisoformat(mn[:10])
        d1 = date.fromisoformat(mx[:10])
        d = d0
        seen = set()
        while d <= d1:
            key = f"{d.year:04d}-{d.month:02d}"
            if key in cal:
                seen.add(key)
            d += timedelta(days=1)
        for key in seen:
            cal[key]["m1_tickers"] += 1

    n_universe = len([t for t in m["per_ticker"]
                      if (m["per_ticker"][t]["m1"].get("written_rows") or 0) > 0])
    for mo in cal:
        cal[mo]["n_universe"] = n_universe

    # 900 ячеек: сигналы/фил/инк по месяцам (by_month из агрегатов)
    cells = []
    for g in GROUPS:
        agg = json.loads((BASE / f"signals_{g}" / "_aggregate" / "group_summary.json").read_text())
        cells.extend(agg["cells"])
    per_month = {mo: {"signals": 0, "FILLED": 0, "INCOMPLETE": 0, "SKIP": 0}
                 for mo in months}
    tot = {"cells": len(cells), "signals": 0, "FILLED": 0, "INCOMPLETE": 0,
           "SKIP": 0, "insufficient_context": 0, "REJECT": 0}
    for c in cells:
        tot["signals"] += c.get("signals", 0)
        tot["FILLED"] += c.get("FILLED", 0)
        tot["INCOMPLETE"] += c.get("INCOMPLETE", 0)
        tot["SKIP"] += c.get("SKIP", 0)
        tot["insufficient_context"] += c.get("insufficient_context", 0)
        tot["REJECT"] += c.get("REJECT", 0)
        for mo, bm in (c.get("by_month") or {}).items():
            if mo in per_month:
                per_month[mo]["FILLED"] += bm.get("filled", 0)
                per_month[mo]["INCOMPLETE"] += bm.get("incomplete", 0)

    def ratio(num, den):
        return round(num / den, 4) if den else None

    out = {
        "window": m["params"],
        "universe": {"n_total": len(m["per_ticker"]), "n_with_m1": n_universe,
                     "n_eligible": elig["n_eligible"],
                     "eligible": elig["eligible_tickers"]},
        "cells_total": tot,
        "share_unknown": {  # INCOMPLETE/(FILLED+INCOMPLETE) — доли неизвестного исхода
            "overall": ratio(tot["INCOMPLETE"], tot["FILLED"] + tot["INCOMPLETE"]),
            "per_month": {mo: ratio(per_month[mo]["INCOMPLETE"],
                                    per_month[mo]["FILLED"] + per_month[mo]["INCOMPLETE"])
                          for mo in months},
        },
        "share_insufficient_context": ratio(tot["insufficient_context"], tot["signals"]),
        "calendar_coverage": {
            mo: {"m1_tickers_with_data": cal[mo]["m1_tickers"],
                 "n_universe_with_m1": cal[mo]["n_universe"],
                 "share_tickers": ratio(cal[mo]["m1_tickers"], cal[mo]["n_universe"]),
                 "FILLED": per_month[mo]["FILLED"],
                 "INCOMPLETE": per_month[mo]["INCOMPLETE"]}
            for mo in months},
    }
    dest = BASE / "coverage_900_calendar.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    print("cells:", tot)
    print("share_unknown (INCOMPLETE/(FILLED+INCOMPLETE)) overall:",
          out["share_unknown"]["overall"])
    print("month | m1_tkrs share FILLED INCOMPLETE unknown%")
    for mo in months:
        cc = out["calendar_coverage"][mo]
        su = out["share_unknown"]["per_month"][mo]
        print(f"{mo} | {cc['m1_tickers_with_data']:>2}/{cc['n_universe_with_m1']} "
              f"{str(cc['share_tickers']):>5} {cc['FILLED']:>6} "
              f"{cc['INCOMPLETE']:>6} {su}")


if __name__ == "__main__":
    main()
