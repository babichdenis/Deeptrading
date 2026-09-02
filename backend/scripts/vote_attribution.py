"""VOTE ATTRIBUTION recompute (AUDIT_ONLY / DATA_RECOMPUTE, read-only).

Источник: canonical July 2026 baseline 5b44f3b383df (config_hash=1c7f75dc44c2aa67).
НЕ перезапускает EngineRunner; не меняет trades/execution. Пересчитывает только
shadow-артефакты + доукомплектовывает недостающее по спеце My3:
  - shrinkage_k = 200, формула weight = (n / (n + 200)) * median_net_bps (walk-forward,
    данные только ДО decision_time T);
  - pair_present (21 пара, допускаются доп. сигналы) + pair_exact (ровно пара);
  - dataset-директория vote_attribution_dataset_v1/ (7 файлов + manifest);
  - vote_attribution_report.md;
  - маркер vote_shadow_scores_INVALID_k20.md для старого k=20.
"""
import sys, os, json, csv, statistics, random, hashlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MSK = ZoneInfo("Europe/Moscow")
RUN_ID = "5b44f3b383df"
CONFIG_HASH = "1c7f75dc44c2aa67"
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_DIR = os.path.join(BACKEND, "reports", RUN_ID)
DS_DIR = os.path.join(RUN_DIR, "vote_attribution_dataset_v1")
SHRINK_K = 200
FORMULA_VERSION = "v1_median_shrink"
BOOTSTRAP_N = 1000
SEED = 42
FN_ORDER = ["bollinger_reclaim", "donchian_breakout", "macd_cross", "pullback_ema",
            "range_compression_breakout", "rsi_reversal", "vwap_reclaim"]


def parse_mask(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("["):
        return sorted(json.loads(raw.replace("'", '"')))
    return [x.strip() for x in raw.split(";") if x.strip()]


def session_bucket(dt: datetime) -> str:
    lt = dt.astimezone(MSK)
    hm = lt.hour * 60 + lt.minute
    if hm < 10 * 60 + 30:
        return "open"
    if hm > 17 * 60:
        return "close"
    return "mid"


def bootstrap_ci(samples: list[float], alpha: float = 0.05) -> list[float] | None:
    if len(samples) < 20:
        return None
    rng = random.Random(SEED)
    meds = []
    for _ in range(BOOTSTRAP_N):
        b = [rng.choice(samples) for _ in range(len(samples))]
        meds.append(statistics.median(b))
    meds.sort()
    return [round(meds[int((alpha / 2) * len(meds))], 2),
            round(meds[int((1 - alpha / 2) * len(meds))], 2)]


def attr_block(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"count": 0}
    nets = [r["net"] for r in rows]
    gross_pos = sum(max(r["gross"], 0) for r in rows)
    gross_neg = sum(max(-r["gross"], 0) for r in rows)
    net_bps = [r["net_bps"] for r in rows]
    exits = {}
    for r in rows:
        exits[r["exit_reason"]] = exits.get(r["exit_reason"], 0) + 1
    mfe = [r["mfe_r"] for r in rows if r["mfe_r"] is not None]
    mae = [r["mae_r"] for r in rows if r["mae_r"] is not None]
    hold = [r["hold_bars"] for r in rows if r["hold_bars"] is not None]
    return {
        "count": n,
        "net": round(sum(nets), 2),
        "net_bps_median": round(statistics.median(net_bps), 2),
        "net_bps_ci95": bootstrap_ci(net_bps),
        "pf": round(gross_pos / gross_neg, 3) if gross_neg > 0 else None,
        "median_net": round(statistics.median(nets), 2),
        "win_rate": round(sum(1 for x in nets if x > 0) / n, 4),
        "exits": exits,
        "median_mfe_r": round(statistics.median(mfe), 3) if mfe else None,
        "median_mae_r": round(statistics.median(mae), 3) if mae else None,
        "median_hold_bars": round(statistics.median(hold), 1) if hold else None,
    }


def load_rows() -> list[dict]:
    intents = list(csv.DictReader(open(os.path.join(RUN_DIR, "entry_intents.csv"))))
    trades = {}
    with open(os.path.join(RUN_DIR, "trades.csv"), newline="") as f:
        for row in csv.DictReader(f):
            trades.setdefault((row["figi"], row["decision_time"].replace("Z", "+00:00"), row["side"]), row)
    rows = []
    for it in intents:
        if it["engine_decision"] != "executed":
            continue
        tr = trades.get((it["figi"], it["decision_time"].replace("Z", "+00:00"),
                         "LONG" if it["side"] == "BUY" else "SHORT"))
        if tr is None:
            continue
        notional = float(tr["entry_notional"] or 0)
        net = float(tr["net_rub"])
        rows.append({
            "intent_id": it["intent_id"], "candidate_id": it["candidate_id"],
            "trade_id": f"{it['figi']}:{tr['entry_time']}", "figi": it["figi"], "side": it["side"],
            "decision_time": datetime.fromisoformat(it["decision_time"].replace("Z", "+00:00")),
            "exit_time": datetime.fromisoformat(tr["exit_time"].replace("Z", "+00:00")),
            "functions": parse_mask(it["functions_mask"]),
            "quorum_count": int(it["quorum_count"] or 0),
            "gross": float(tr["gross_rub"]), "net": net,
            "net_bps": (net / notional * 10000.0) if notional else 0.0,
            "exit_reason": tr["exit_reason"],
            "mfe_r": float(tr["mfe_r"]) if tr["mfe_r"] else None,
            "mae_r": float(tr["mae_r"]) if tr["mae_r"] else None,
            "hold_bars": float(tr["hold_bars"]) if tr["hold_bars"] else None,
            "session": session_bucket(datetime.fromisoformat(it["decision_time"].replace("Z", "+00:00"))),
        })
    return rows


def compute_vol_regime(rows: list[dict]) -> None:
    from app.services.research_pack import _load_candles
    from app.services.ensemble import resample, TF_SECONDS

    def _atr_day_map(figi: str) -> dict[str, bool]:
        candles = _load_candles(figi, datetime(2026, 7, 1, tzinfo=timezone.utc),
                                datetime(2026, 8, 1, tzinfo=timezone.utc))
        c5 = resample(candles, TF_SECONDS["5min"])
        atr = [None] * len(c5)
        trs = []
        for i in range(1, len(c5)):
            h, l, pc = c5[i].high, c5[i].low, c5[i - 1].close
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
            if len(trs) > 14:
                trs.pop(0)
            if i >= 14:
                atr[i] = sum(trs) / 14
        day_atr = {}
        for i, c in enumerate(c5):
            d = c.ts.astimezone(MSK).date().isoformat()
            if atr[i] is not None:
                day_atr[d] = atr[i]
        days = sorted(day_atr)
        roll = {}
        for idx, d in enumerate(days):
            prev = days[max(0, idx - 20):idx]
            roll[d] = statistics.median([day_atr[x] for x in prev]) if prev else None
        return {d: (roll[d] is not None and day_atr[d] > roll[d]) for d in days}

    vol_map = {}
    for figi in sorted({r["figi"] for r in rows}):
        vol_map[figi] = _atr_day_map(figi)
    for r in rows:
        d = r["decision_time"].astimezone(MSK).date().isoformat()
        v = vol_map.get(r["figi"], {}).get(d)
        r["vol_regime"] = "hi_vol" if v is True else ("lo_vol" if v is False else "warmup")


def main() -> None:
    rows = load_rows()
    print(f"matched executed rows: {len(rows)}", flush=True)
    compute_vol_regime(rows)

    fn_names = sorted({f for r in rows for f in r["functions"]})

    # ---------- attribution (k-независимая) ----------
    def per_fn(rows_subset: list[dict]) -> dict:
        return {fn: attr_block([r for r in rows_subset if fn in r["functions"]])
                for fn in fn_names}

    attribution = {
        "run_id": RUN_ID, "window": "2026-07-01..2026-07-31",
        "config_hash": CONFIG_HASH,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 2.0, "risk_reward": 2.0}},
        "capital_per_position": 10000, "schema": "vote_attribution_v1",
        "note": "read-only; oracle не использовался; execution не менялся",
        "per_function": per_fn(rows),
        "per_function_by_figi": {f: per_fn([r for r in rows if r["figi"] == f]) for f in sorted({r["figi"] for r in rows})},
        "per_function_by_side": {s: per_fn([r for r in rows if r["side"] == s]) for s in ("BUY", "SELL")},
        "per_function_by_session": {s: per_fn([r for r in rows if r["session"] == s]) for s in ("open", "mid", "close")},
        "per_function_by_vol": {v: per_fn([r for r in rows if r["vol_regime"] == v]) for v in ("hi_vol", "lo_vol", "warmup")},
    }

    # pair_present (оба присутствуют, допускаются другие) и pair_exact (ровно пара).
    # Включаем ВСЕ 21 пару (count=0 для невстреченных в executed) — структура полная.
    pairs = [(a, b) for i, a in enumerate(fn_names) for b in fn_names[i + 1:]]
    pair_present, pair_exact = {}, {}
    for f1, f2 in pairs:
        sub_p = [r for r in rows if f1 in r["functions"] and f2 in r["functions"]]
        sub_e = [r for r in rows if set(r["functions"]) == {f1, f2}]
        pair_present[f"{f1}+{f2}"] = attr_block(sub_p)
        pair_exact[f"{f1}+{f2}"] = attr_block(sub_e)
    attribution["per_pair_present"] = pair_present
    attribution["per_pair_exact"] = pair_exact

    exact = {}
    for r in rows:
        exact.setdefault("+".join(r["functions"]), []).append(r)
    attribution["per_exact_combination"] = {k: attr_block(v) for k, v in sorted(exact.items())}
    attribution["totals"] = attr_block(rows)

    # ---------- walk-forward shadow weights (k=200, median) ----------
    ordered = sorted(rows, key=lambda r: r["decision_time"])
    days = sorted({r["decision_time"].astimezone(MSK).date().isoformat() for r in ordered})

    def train_weights(cutoff: datetime) -> dict[str, float]:
        acc: dict[str, list[float]] = {}
        for r in rows:
            if r["exit_time"] < cutoff:
                for fn in r["functions"]:
                    acc.setdefault(fn, []).append(r["net_bps"])
        w = {}
        for fn, vals in acc.items():
            n = len(vals)
            w[fn] = (n / (n + SHRINK_K)) * statistics.median(vals) if vals else 0.0
        return w

    day_start = {d: datetime(*map(int, d.split("-")), tzinfo=MSK).astimezone(timezone.utc) for d in days}

    score_rows = []
    weights_by_day: dict[str, dict[str, float]] = {}
    train_end_by_day: dict[str, str] = {}
    for d in days:
        w = train_weights(day_start[d])
        weights_by_day[d] = w
        train_end_by_day[d] = max((x["exit_time"] for x in rows if x["exit_time"] < day_start[d]),
                                  default=day_start[d]).isoformat()
        for r in ordered:
            if r["decision_time"].astimezone(MSK).date().isoformat() != d:
                continue
            if not r["functions"]:
                continue
            score = sum(w.get(fn, 0.0) for fn in r["functions"]) / len(r["functions"])
            score_rows.append({
                "intent_id": r["intent_id"], "candidate_id": r["candidate_id"],
                "trade_id": r["trade_id"], "figi": r["figi"], "side": r["side"],
                "decision_time": r["decision_time"].isoformat(),
                "functions": ";".join(r["functions"]), "quorum_count": r["quorum_count"],
                "shadow_score": round(score, 6),
                "weights_snapshot": json.dumps({k: round(v, 6) for k, v in sorted(w.items())}),
                "training_period_end": train_end_by_day[d],
                "weight_timestamp": r["decision_time"].isoformat(),
            })

    weights_snapshot_ts = {d: {k: round(v, 6) for k, v in sorted(w.items())}
                           for d, w in weights_by_day.items()}
    attribution["shadow_weights"] = {"formula_version": FORMULA_VERSION,
                                     "shrinkage_k": SHRINK_K,
                                     "weights_by_day": weights_snapshot_ts,
                                     "training_period_end_by_day": train_end_by_day}
    with open(os.path.join(RUN_DIR, "vote_attribution.json"), "w") as f:
        json.dump(attribution, f, ensure_ascii=False, indent=2)

    # ---------- dataset dir ----------
    os.makedirs(DS_DIR, exist_ok=True)

    def _write_csv(name: str, rows_out: list[dict]) -> None:
        if not rows_out:
            return
        cols = []
        for r in rows_out:
            for k in r.keys():
                if k not in cols:
                    cols.append(k)
        with open(os.path.join(DS_DIR, name), "w", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            wr.writeheader(); wr.writerows(rows_out)

    # function_attribution.csv
    fn_rows = []
    for fn in fn_names:
        b = attribution["per_function"][fn]
        fn_rows.append({"function": fn, **{k: v for k, v in b.items() if k != "exits"}})
    _write_csv("function_attribution.csv", fn_rows)

    # pair_present_attribution.csv
    pp_rows = [{"pair": k, **{kk: vv for kk, vv in v.items() if kk != "exits"}}
               for k, v in sorted(pair_present.items())]
    _write_csv("pair_present_attribution.csv", pp_rows)
    pe_rows = [{"pair": k, **{kk: vv for kk, vv in v.items() if kk != "exits"}}
               for k, v in sorted(pair_exact.items())]
    _write_csv("pair_exact_attribution.csv", pe_rows)

    # intent_vote_features.csv (7 бинарных признаков + outcome)
    feat_rows = []
    for r in ordered:
        feat_rows.append({
            "intent_id": r["intent_id"], "candidate_id": r["candidate_id"],
            "trade_id": r["trade_id"], "figi": r["figi"], "side": r["side"],
            "decision_time": r["decision_time"].isoformat(),
            "session": r["session"], "vol_regime": r["vol_regime"],
            "quorum_count": r["quorum_count"],
            **{f"fn_{fn}": (1 if fn in r["functions"] else 0) for fn in FN_ORDER},
            "net_bps": round(r["net_bps"], 2), "net": round(r["net"], 2),
        })
    _write_csv("intent_vote_features.csv", feat_rows)

    # shadow_weights.json
    with open(os.path.join(DS_DIR, "shadow_weights.json"), "w") as f:
        json.dump({"formula_version": FORMULA_VERSION, "shrinkage_k": SHRINK_K,
                   "weights_by_day": weights_snapshot_ts,
                   "training_period_end_by_day": train_end_by_day}, f, indent=2)
    # vote_shadow_scores.csv (в dataset dir)
    with open(os.path.join(DS_DIR, "vote_shadow_scores.csv"), "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(score_rows[0].keys()))
        wr.writeheader(); wr.writerows(score_rows)

    manifest = {
        "run_id": RUN_ID, "config_hash": CONFIG_HASH,
        "window": "2026-07-01..2026-07-31",
        "training_cutoff_rule": "weights for decision T use only closed trades with exit_time < day_start(T)",
        "shrinkage_k": SHRINK_K, "formula": "weight = (n/(n+k)) * median_net_bps",
        "formula_version": FORMULA_VERSION,
        "n_executed_intents": len(rows),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": {fn: hashlib.sha256(open(os.path.join(DS_DIR, fn), "rb").read()).hexdigest()[:16]
                  for fn in sorted(os.listdir(DS_DIR))},
    }
    with open(os.path.join(DS_DIR, "manifest.json"), "w") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # ---------- INVALID marker for old k=20 (сохраняем старый файл + before-checksum) ----------
    old_scores = os.path.join(RUN_DIR, "vote_shadow_scores.csv")
    k20_backup = os.path.join(RUN_DIR, "vote_shadow_scores_k20_INVALID.csv")
    if os.path.exists(old_scores) and not os.path.exists(k20_backup):
        with open(old_scores, "rb") as f:
            old_sha = hashlib.sha256(f.read()).hexdigest()[:16]
        import shutil
        shutil.copy2(old_scores, k20_backup)
    else:
        old_sha = hashlib.sha256(open(k20_backup, "rb").read()).hexdigest()[:16]
    # перезапись корневого vote_shadow_scores.csv на canonical k=200 (спека п.2)
    with open(old_scores, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(score_rows[0].keys()))
        wr.writeheader(); wr.writerows(score_rows)
    with open(os.path.join(RUN_DIR, "vote_shadow_scores_INVALID_k20.md"), "w") as f:
        f.write("# INVALID FOR RANKING\n\n"
                f"Старый корневой `vote_shadow_scores.csv` (shrinkage_k=20) скопирован в "
                f"`vote_shadow_scores_k20_INVALID.csv` (before sha256: {old_sha}).\n"
                "Канонический `shrinkage_k=200` по спеце владельца/My3: "
                "корневой `vote_shadow_scores.csv` и `vote_attribution_dataset_v1/`.\n")

    # ---------- report md ----------
    tpl = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vote_attribution_report_tpl.md")
    with open(tpl) as f:
        report_md = f.read()
    with open(os.path.join(RUN_DIR, "vote_attribution_report.md"), "w") as f:
        f.write(report_md)

    print(json.dumps({"rows": len(rows), "days": days, "score_rows": len(score_rows),
                      "pair_present": len(pair_present), "pair_exact": len(pair_exact),
                      "ds_dir": DS_DIR, "manifest_sha": manifest["files"]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
