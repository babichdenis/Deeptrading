#!/usr/bin/env python
"""Аналитический каталог независимости движков (по событиям).

Пайплайн: events → overlap graph → families → canonical representative.
Ничего не удаляет и не меняет: raw events остаются, каталог — отдельный слой.

Правила:
  - ребро семьи: (overlap>=0.7 и same_side>=0.9) или (containment>=0.95 и same_side>=0.9);
  - antipode: same_side<=0.05 при common>=300 — зеркальная логика, в семьи НЕ мержится;
  - canonical: НЕ по med+24, а технически — движок с максимальным покрытием
    (в него вложено больше других), при равенстве — имя без meta-маркеров
    (canon/ensemble/vote/hub-обёртки штрафуются), затем лексикографически;
  - отношения: EXACT_DUPLICATE / SUBSET / NEAR_DUPLICATE / MEMBER;
  - тип объекта: STATE (occ>=0.95), HYBRID_STATE (occ>=0.6), EVENT (иначе).
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import text  # noqa: E402

from app.lab.data import engine_sync  # noqa: E402

OUT = BACKEND / "reports" / "signal_lab" / "events"
META_MARKERS = ("canon", "ensemble", "vote")
COMMON_MIN = 300


def load_events(eng, rid):
    with eng.connect() as c:
        rows = c.execute(text(
            "SELECT strategy_id, figi, tf, start_ts, side FROM lab_signal_event_runs "
            "WHERE run_id = :r"), {"r": rid}).fetchall()
    per = defaultdict(set)
    keys = defaultdict(list)
    for st, figi, tf, start, side in rows:
        per[st].add((figi, tf, start))
        keys[(figi, tf, start)].append((st, side))
    return per, keys


def load_occ(eng, rid):
    ub = {r[0]: int(r[1]) for r in (eng.connect().execute(text(
        "SELECT tf, count(DISTINCT (figi, bar_ts)) FROM lab_signal_events "
        "WHERE run_id = :r GROUP BY 1"), {"r": rid}).fetchall())}
    rows = eng.connect().execute(text(
        "SELECT strategy_id, sum(duration_bars) FROM lab_signal_event_runs "
        "WHERE run_id = :r GROUP BY 1"), {"r": rid}).fetchall()
    q = eng.connect().execute(text(
        "SELECT strategy_id, tf, sum(duration_bars), count(*) "
        "FROM lab_signal_event_runs WHERE run_id = :r GROUP BY 1,2"), {"r": rid}).fetchall()
    occ_by = defaultdict(dict)
    for st, tf, bars, _ev in q:
        tot = ub.get(tf) or 0
        if tot:
            occ_by[st][tf] = bars / tot
    occ_max = {st: max(v.values()) if v else 0.0 for st, v in occ_by.items()}
    ev_by = {st: sum(int(r[3]) for r in q if r[0] == st) for st in occ_max}
    return occ_max, ev_by


def build_pairs(per, keys):
    engines = sorted(per)
    idx = {e: i for i, e in enumerate(engines)}
    common = defaultdict(int)
    same = defaultdict(int)
    for _k, lst in keys.items():
        uniq = {}
        for st, sd in lst:
            uniq.setdefault(st, sd)
        ids = sorted((idx[st], sd) for st, sd in uniq.items())
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i][0], ids[j][0]
                p = (a, b)
                common[p] += 1
                if ids[i][1] == ids[j][1]:
                    same[p] += 1
    out = []
    for (a, b), x in common.items():
        if x < COMMON_MIN:
            continue
        na, nb = len(per[engines[a]]), len(per[engines[b]])
        out.append({"a": engines[a], "b": engines[b], "x": x,
                    "ca": x / na, "cb": x / nb, "ovl": x / min(na, nb),
                    "ss": same[(a, b)] / x})
    return engines, out


def pick_canonical(members, per, pairs_by_pair):
    def coverage(e):
        cov = 0.0
        for m in members:
            if m == e:
                continue
            p = pairs_by_pair.get((e, m)) or pairs_by_pair.get((m, e))
            if p:
                cov += (p["ca"] if p["a"] == m else p["cb"])
        return cov

    def penalty(e):
        low = e.lower()
        return (1 if any(mk in low for mk in META_MARKERS) else 0,
                1 if low.endswith("_hub") and "hub" in low else 0, e)
    return sorted(members, key=lambda e: (-coverage(e), penalty(e)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    args = ap.parse_args()
    eng = engine_sync()
    rid = args.run_id
    print("run_id=", rid, flush=True)
    per, keys = load_events(eng, rid)
    occ, ev_total = load_occ(eng, rid)
    print(f"движков: {len(per)}; событий: {sum(len(v) for v in per.values()):,}", flush=True)

    engines, pairs = build_pairs(per, keys)
    print(f"пар с common>={COMMON_MIN}: {len(pairs)}", flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "pairs_all.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["a", "b", "common", "contain_a_in_b", "contain_b_in_a",
                    "overlap", "same_side"])
        for p_ in sorted(pairs, key=lambda q: -q["x"]):
            w.writerow([p_["a"], p_["b"], p_["x"], round(p_["ca"], 4),
                        round(p_["cb"], 4), round(p_["ovl"], 4), round(p_["ss"], 4)])
    by_pair = {}
    for p in pairs:
        by_pair[(p["a"], p["b"])] = p

    parent = {e: e for e in engines}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    antipodes = defaultdict(list)
    has_positive = defaultdict(bool)
    for p in pairs:
        a, b = p["a"], p["b"]
        if p["ss"] >= 0.5:
            has_positive[a] = has_positive[b] = True
        if p["ss"] <= 0.05:
            antipodes[a].append((b, p))
            antipodes[b].append((a, p))
            continue
        strong = (p["ovl"] >= 0.7 and p["ss"] >= 0.9) or \
                 (max(p["ca"], p["cb"]) >= 0.95 and p["ss"] >= 0.9)
        if strong:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

    families = defaultdict(list)
    for e in engines:
        families[find(e)].append(e)

    notes = {
        "ose_rsi_contrtrend": "data_quality: ATR/price аномалия, не удалять",
        "parabolic_price_channel_hub": "excluded: сломан, MFE_med=0",
        "parabolic_sar_hub": "excluded: state bug (сигналы только в начале ряда)",
        "parabolic_bollinger_hub": "excluded: 0 сигналов",
        "williams_range_hub": "анти-зеркало momentum/breakout семейства (ss~0 на топ-парах)",
    }
    cat_rows = []
    sum_rows = []
    fid = 0
    for root, members in families.items():
        members = sorted(members)
        anti = (len(members) == 1 and members[0] in antipodes
                and not has_positive[members[0]])
        if anti:
            e = members[0]
            counterpart = max(antipodes[e], key=lambda t: t[1]["x"]) if antipodes[e] else None
            cat_rows.append([f"F{fid:02d}", e, "MIRROR", "-", "", "",
                             round(occ.get(e, 0), 4), ev_total.get(e, 0),
                             f"зеркало {counterpart[0]} (ss={counterpart[1]['ss']:.2f})"
                             if counterpart else "зеркало"])
            sum_rows.append([f"F{fid:02d}", e, "MIRROR", 1, ev_total.get(e, 0),
                             round(occ.get(e, 0), 3), "независимой альфы нет"])
            fid += 1
            continue
        canon = pick_canonical(members, per, by_pair)[0]
        for m in members:
            p = by_pair.get((canon, m)) or by_pair.get((m, canon))
            if m == canon:
                rel = "CANONICAL"
            elif p is None:
                rel = "MEMBER"
            else:
                c_m = p["ca"] if p["a"] == m else p["cb"]      # containment m в canon
                c_c = p["cb"] if p["a"] == m else p["ca"]      # containment canon в m
                size_ratio = min(len(per[m]), len(per[canon])) / max(len(per[m]), len(per[canon]))
                if c_m >= 0.98 and c_c >= 0.98 and p["ss"] >= 0.98 and size_ratio >= 0.98:
                    rel = "EXACT_DUPLICATE"
                elif c_m >= 0.9:
                    rel = "SUBSET"
                elif p["ovl"] >= 0.7 and p["ss"] >= 0.9:
                    rel = "NEAR_DUPLICATE"
                else:
                    rel = "MEMBER"
            cat_rows.append([f"F{fid:02d}", canon, rel, m,
                             round(p["ovl"], 3) if p else "",
                             round(p["ss"], 3) if p else "",
                             round(occ.get(m, 0), 4), ev_total.get(m, 0),
                             notes.get(m, "")])
        occ_max = max(occ.get(m, 0) for m in members)
        typ = "STATE" if occ_max >= 0.95 else ("HYBRID_STATE" if occ_max >= 0.6 else "EVENT")
        rels = [r[2] for r in cat_rows if r[0] == f"F{fid:02d}"]
        rel_s = ",".join(sorted(set(rels) - {"CANONICAL"}))
        if canon in notes:
            rel_s = (rel_s + " | " + notes[canon]).strip(" |")
        sum_rows.append([f"F{fid:02d}", canon, typ, len(members),
                         ev_total.get(canon, 0), round(occ.get(canon, 0), 3), rel_s])
        fid += 1

    cat_rows.sort(key=lambda r: (r[0], 0 if r[2] == "CANONICAL" else 1, r[3]))
    sum_rows.sort(key=lambda r: -r[4] if isinstance(r[4], int) else 0)
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "family_catalog.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["family_id", "canonical_engine", "relationship", "member_engine",
                    "overlap_to_canonical", "same_side_to_canonical", "occupancy",
                    "member_events", "note"])
        w.writerows(cat_rows)
    with open(OUT / "family_summary.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["family_id", "canonical_engine", "object_type", "members",
                    "canonical_events", "canonical_occupancy", "relations"])
        w.writerows(sum_rows)
    print(f"семей всего: {len(sum_rows)}", flush=True)
    print("=== каталог (canonical | тип | members | events | occ | relations):", flush=True)
    for r in sum_rows:
        print(f"  {r[0]} {r[1]:26s} {r[2]:12s} m={r[3]:>2} ev={r[4]:>7,} "
              f"occ={r[5]:.2f} {r[6]}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
