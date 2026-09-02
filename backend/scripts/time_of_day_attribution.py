"""B1 TIME_OF_DAY_ATTRIBUTION (SHADOW / READ-ONLY).

Bucket-анализ сделок canonical runs по decision_time (MSK, 30-мин корзины)
внутри main session 10:00–18:45. Сравнение между периодами:
  - Июль 2026 (5b44f3b383df, полный pack)
  - Март–Апр 2026 (57b6244ee3eb, trades.csv)
  - Май–Июнь 2026 — ТОЛЬКО агрегаты (DRAFT), trade-level ограничен (отметка).
Артефакты: reports/time_of_day_attribution.json/.csv/.md
"""
import sys, os, json, csv, statistics
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MSK = ZoneInfo("Europe/Moscow")
REPORTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
JULY = os.path.join(REPORTS, "5b44f3b383df")
MARA = os.path.join(REPORTS, "57b6244ee3eb")


def bucket_key(dt: datetime) -> str:
    lt = dt.astimezone(MSK)
    half = 30 * (lt.hour * 2 + (1 if lt.minute >= 30 else 0))
    return f"{half//60:02d}:{(half%60):02d}"


def load_trades(pack_dir: str) -> list[dict]:
    trades = []
    with open(os.path.join(pack_dir, "trades.csv"), newline="") as f:
        for r in csv.DictReader(f):
            trades.append(r)
    return trades


def build(rows: list[dict]) -> dict:
    out = {}
    for r in rows:
        dt = datetime.fromisoformat(r["decision_time"].replace("Z", "+00:00"))
        b = bucket_key(dt)
        bd = out.setdefault(b, {"count": 0, "gross": 0.0, "commission": 0.0, "slippage": 0.0,
                                "net": 0.0, "exits": {}, "mfe": [], "mae": [], "hold": []})
        bd["count"] += 1
        g = float(r["gross_rub"]); bd["gross"] += g
        bd["commission"] += float(r["commission_rub"]); bd["slippage"] += float(r["slippage_rub"])
        bd["net"] += float(r["net_rub"])
        bd["exits"][r["exit_reason"]] = bd["exits"].get(r["exit_reason"], 0) + 1
        if r.get("mfe_r"): bd["mfe"].append(float(r["mfe_r"]))
        if r.get("mae_r"): bd["mae"].append(float(r["mae_r"]))
        if r.get("hold_bars"): bd["hold"].append(float(r["hold_bars"]))
    for b, bd in out.items():
        bd["net_per_trade"] = round(bd["net"] / bd["count"], 2)
        bd["pf"] = None
        bd["median_mfe"] = round(statistics.median(bd["mfe"]), 3) if bd["mfe"] else None
        bd["median_mae"] = round(statistics.median(bd["mae"]), 3) if bd["mae"] else None
        bd["median_hold"] = round(statistics.median(bd["hold"]), 1) if bd["hold"] else None
        bd.pop("mfe"); bd.pop("mae"); bd.pop("hold")
    return out


def main() -> None:
    periods = {}

    july = load_trades(JULY)
    periods["2026-07"] = {"pack": "5b44f3b383df", "trades": len(july), "by_bucket": build(july)}

    mara = load_trades(MARA)
    periods["2026-03_04"] = {"pack": "57b6244ee3eb", "trades": len(mara), "by_bucket": build(mara)}

    # Май–Июнь: trade-level недоступен (DRAFT агрегаты)
    periods["2026-05_06"] = {"pack": "DRAFT_aggregates_only",
                             "note": "trade-level недоступен (e2/e5 DRAFT без per-trade); bucket-анализ ограничен",
                             "trades": None, "by_bucket": {}}

    # combined (июль + мар-апр; май-июнь исключён из-за отсутствия trade-level)
    combined = {}
    for p in ("2026-07", "2026-03_04"):
        for b, bd in periods[p]["by_bucket"].items():
            cb = combined.setdefault(b, {"count": 0, "gross": 0.0, "net": 0.0})
            cb["count"] += bd["count"]; cb["gross"] += bd["gross"]; cb["net"] += bd["net"]
    for b, cb in combined.items():
        cb["net_per_trade"] = round(cb["net"] / cb["count"], 2)

    result = {"schema": "time_of_day_attribution_v1", "tz": "Europe/Moscow",
              "bucket_min": 30, "main_session": "10:00-18:45 MSK",
              "note": "SHADOW/read-only; не меняет EngineRunner; не рекомендуется time-filter без повторяемости",
              "periods": periods, "combined_2026_07_and_03_04": combined,
              "generated_at": datetime.now(timezone.utc).isoformat()}

    with open(os.path.join(REPORTS, "time_of_day_attribution.json"), "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # CSV: combined + по периодам
    all_buckets = sorted({b for p in ("2026-07", "2026-03_04") for b in periods[p]["by_bucket"]})
    with open(os.path.join(REPORTS, "time_of_day_attribution.csv"), "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["bucket_msk", "jul_count", "jul_net", "jul_net_pt", "mara_count", "mara_net",
                     "mara_net_pt", "comb_count", "comb_net", "comb_net_pt"])
        for b in all_buckets:
            j = periods["2026-07"]["by_bucket"].get(b, {})
            m = periods["2026-03_04"]["by_bucket"].get(b, {})
            c = combined.get(b, {})
            wr.writerow([b, j.get("count", 0), round(j.get("net", 0), 2), j.get("net_per_trade", 0),
                         m.get("count", 0), round(m.get("net", 0), 2), m.get("net_per_trade", 0),
                         c.get("count", 0), round(c.get("net", 0), 2), c.get("net_per_trade", 0)])

    # MD report
    md = ["# TIME OF DAY ATTRIBUTION (SHADOW, read-only)", "",
          f"Сгенерировано: {result['generated_at']}", "",
          "## Корзины (decision_time MSK, 30 мин; main session 10:00-18:45)", "",
          "| bucket | Jul count | Jul net | Jul net/trade | MarApr count | MarApr net | MarApr net/trade |",
          "|---|---|---|---|---|---|---|"]
    for b in all_buckets:
        j = periods["2026-07"]["by_bucket"].get(b, {}); m = periods["2026-03_04"]["by_bucket"].get(b, {})
        md.append(f"| {b} | {j.get('count',0)} | {round(j.get('net',0),2)} | {j.get('net_per_trade')} | "
                  f"{m.get('count',0)} | {round(m.get('net',0),2)} | {m.get('net_per_trade')} |")
    md += ["", "## Стабильность (Jul vs Mar-Apr): наличие прибыльных/убыточных корзин на ≥2 периодах", ""]
    stable = []
    for b in all_buckets:
        jn = periods["2026-07"]["by_bucket"].get(b, {}).get("net", 0)
        mn = periods["2026-03_04"]["by_bucket"].get(b, {}).get("net", 0)
        if (jn > 0 and mn > 0) or (jn <= 0 and mn <= 0):
            stable.append((b, jn, mn))
    if stable:
        md.append("Повторяющиеся корзины: " + ", ".join(f"{b}({round(j,1)}/{round(m,1)})" for b, j, m in stable))
    else:
        md.append("Повторяющихся корзин на 2 периодах НЕТ.")
    md += ["", "## Май–Июнь 2026", "trade-level недоступен (DRAFT только агрегаты) — bucket-анализ ограничен.", "",
           "## Вывод", "НЕ рекомендуется time-filter без повторяемости рисунка на ≥2 независимых периодах. "
                        "Здесь 2 периода (Jul, Mar-Apr) — см. таблицу выше."]
    with open(os.path.join(REPORTS, "time_of_day_attribution.md"), "w") as f:
        f.write("\n".join(md))
    print(json.dumps({"periods": {k: v["trades"] for k, v in periods.items()},
                      "buckets": len(all_buckets)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
