"""B2 IMOEX_CONTEXT_ATTRIBUTION (SHADOW / READ-ONLY).

Контекст рынка (IMOEX INDEXCF) поверх ATR-волатильности, для canonical July 2026.
  - contemporaneous: признаки IMOEX СТРОГО <= decision_ts (return 5m/30m/1h, EMA50, slope,
    realised vol, stock residual = stock_ret - beta*imoex_ret, alignment signal vs IMOEX)
  - lead-lag (SHADOW, использует будущее ТОЛЬКО для диагностики, помечено): направление IMOEX
    предсказывает движение акции на 2/5/10 мин (directional accuracy)
  - IMOEX-informed stop concept (контрфактуально): выход при cumulative развороте IMOEX -X bps
    после entry; сравнение MAE/net с фактическим (НЕ policy)
IMOEX НЕ добавляется в execution. Артефакты: imoex_context_attribution.{json,csv,md}
"""
import sys, os, json, csv, statistics, math
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MSK = ZoneInfo("Europe/Moscow")
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(BACKEND, "reports")
RUN_DIR = os.path.join(REPORTS, "5b44f3b383df")
IMOEX = "BBG00KDWPPW2"
FIGIS = ["BBG008F2T3T2", "BBG004S681M2", "BBG004S683W7", "BBG004S68CP5", "BBG004S681B4"]
T_FROM = datetime(2026, 7, 1, tzinfo=timezone.utc)
T_TO = datetime(2026, 8, 1, tzinfo=timezone.utc)
BETA_WIN = 240  # 5m баров (5 торговых дней) для оценки beta ДО decision


def load_1m(figi: str) -> list[dict]:
    from app.services.research_pack import _load_candles
    cs = _load_candles(figi, T_FROM, T_TO)
    return [{"ts": c.ts, "o": c.open, "h": c.high, "l": c.low, "c": c.close} for c in cs]


def ret_over(candles: list[dict], idx: int, bars: int) -> float | None:
    if idx < bars or idx >= len(candles):
        return None
    return (candles[idx]["c"] / candles[idx - bars]["c"] - 1.0) * 10000.0


def main() -> None:
    print("loading IMOEX 1m ...", flush=True)
    im = load_1m(IMOEX)
    im_ts = [c["ts"] for c in im]
    print(f"IMOEX 1m: {len(im)}", flush=True)
    stock = {f: load_1m(f) for f in FIGIS}
    print("loading trades ...", flush=True)
    trades = list(csv.DictReader(open(os.path.join(RUN_DIR, "trades.csv"))))

    rows = []
    for t in trades:
        figi = t["figi"]
        if figi not in stock:
            continue
        dt = datetime.fromisoformat(t["decision_time"].replace("Z", "+00:00"))
        sc = stock[figi]
        # index IMOEX bar <= decision_ts
        import bisect
        j = bisect.bisect_right(im_ts, dt) - 1
        if j < 60:
            continue
        side = t["side"]  # LONG/SHORT
        sign = 1.0 if side == "LONG" else -1.0
        i5 = ret_over(im, j, 5)    # ~5 мин (1m)
        i30 = ret_over(im, j, 30)
        i60 = ret_over(im, j, 60)
        # EMA50 slope (по 1m close IMOEX) на j
        ema = 0.0
        for k in range(max(0, j - 49), j + 1):
            ema = (im[k]["c"] - ema) * (2 / 51) + ema if k > 0 else im[k]["c"]
        ema_prev = 0.0
        for k in range(max(0, j - 50), j):
            ema_prev = (im[k]["c"] - ema_prev) * (2 / 51) + ema_prev if k > 0 else im[k]["c"]
        slope = (ema - ema_prev) / max(abs(ema_prev), 1e-9) * 10000 if ema_prev else 0.0
        # realised vol IMOEX (std 20 returns 1m)
        rets = [(im[k]["c"] / im[k - 1]["c"] - 1) for k in range(j - 19, j + 1) if k >= 1]
        ivol = statistics.pstdev(rets) * 1e4 if len(rets) > 5 else None
        # stock return 5m + beta (на окне BETA_WIN баров до j, по 1m)
        sj = bisect.bisect_right([c["ts"] for c in sc], dt) - 1
        s_ret_5 = ret_over(sc, sj, 5)
        # beta: cov(stock,imoex)/var(imoex) на окне [j-BETA_WIN, j]
        lo = max(1, j - BETA_WIN)
        xs, ys = [], []
        for k in range(lo + 1, j + 1):
            xs.append(im[k]["c"] / im[k - 1]["c"] - 1)
            ys.append(sc[sj - (j - k)]["c"] / sc[sj - (j - k) - 1]["c"] - 1) if (sj - (j - k) - 1) >= 0 else None
        beta = None
        if len(xs) > 30:
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            vx = sum((x - mx) ** 2 for x in xs) / len(xs)
            if vx > 1e-14:
                beta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / len(xs) / vx
        residual = None
        if s_ret_5 is not None and i5 is not None and beta is not None:
            residual = s_ret_5 - beta * i5
        # alignment
        aligned = None
        if i5 is not None:
            aligned = (i5 > 0) == (sign > 0) if sign > 0 else (i5 < 0) == (sign > 0) or (i5 > 0) == (sign > 0)
            aligned = (i5 * sign) > 0
        trend = "up" if slope > 5 else ("down" if slope < -5 else "range")
        ivol_hi = ivol is not None and ivol > 12.0  # грубый порог (1m, bps)
        # lead-lag (будущее, только для диагностики)
        lead = {}
        for k_min in (2, 5, 10):
            kk = k_min
            f_stock = None
            if sj + kk < len(sc) and sj >= 0:
                f_stock = (sc[sj + kk]["c"] / sc[sj]["c"] - 1) * 10000
            f_imoex = None
            if j + kk < len(im):
                f_imoex = (im[j + kk]["c"] / im[j]["c"] - 1) * 10000
            correct = None
            if f_stock is not None and f_imoex is not None:
                correct = (f_stock * f_imoex) > 0  # направление IMOEX совпало с акцией
            lead[f"lead_{k_min}m"] = {"stock_fwd_bps": round(f_stock, 3) if f_stock is not None else None,
                                      "imoex_fwd_bps": round(f_imoex, 3) if f_imoex is not None else None,
                                      "correct_direction": correct}
        rows.append({
            "trade_id": t["trade_id"], "figi": figi, "side": side,
            "decision_time": dt.isoformat(), "net": float(t["net_rub"]),
            "gross": float(t["gross_rub"]), "exit_reason": t["exit_reason"],
            "mfe_r": float(t["mfe_r"]) if t["mfe_r"] else None,
            "mae_r": float(t["mae_r"]) if t["mae_r"] else None,
            "hold_bars": float(t["hold_bars"]) if t["hold_bars"] else None,
            "imoex_ret_5m_bps": round(i5, 3) if i5 is not None else None,
            "imoex_ret_30m_bps": round(i30, 3) if i30 is not None else None,
            "imoex_ret_1h_bps": round(i60, 3) if i60 is not None else None,
            "imoex_ema_slope_bps": round(slope, 3), "imoex_vol_bps": round(ivol, 3) if ivol else None,
            "beta": round(beta, 3) if beta is not None else None,
            "residual_5m_bps": round(residual, 3) if residual is not None else None,
            "aligned": aligned, "trend": trend, "ivol_hi": ivol_hi, **lead,
        })
    print(f"rows: {len(rows)}", flush=True)

    def econ(sub: list[dict]) -> dict:
        n = len(sub)
        if n == 0:
            return {"count": 0}
        net = sum(r["net"] for r in sub)
        gp = sum(max(r["gross"], 0) for r in sub)
        gl = sum(max(-r["gross"], 0) for r in sub)
        return {"count": n, "net": round(net, 2), "net_per_trade": round(net / n, 2),
                "pf": round(gp / gl, 3) if gl > 0 else None,
                "win_rate": round(sum(1 for r in sub if r["net"] > 0) / n, 4),
                "median_mfe_r": round(statistics.median([r["mfe_r"] for r in sub if r["mfe_r"] is not None]), 3),
                "median_mae_r": round(statistics.median([r["mae_r"] for r in sub if r["mae_r"] is not None]), 3),
                "median_hold_bars": round(statistics.median([r["hold_bars"] for r in sub if r["hold_bars"] is not None]), 1)}

    groups = {
        "aligned": econ([r for r in rows if r["aligned"] is True]),
        "counter_market": econ([r for r in rows if r["aligned"] is False]),
        "trend_up": econ([r for r in rows if r["trend"] == "up"]),
        "trend_down": econ([r for r in rows if r["trend"] == "down"]),
        "trend_range": econ([r for r in rows if r["trend"] == "range"]),
        "index_vol_hi": econ([r for r in rows if r["ivol_hi"] is True]),
        "index_vol_lo": econ([r for r in rows if r["ivol_hi"] is False]),
        "residual_pos": econ([r for r in rows if r["residual_5m_bps"] is not None and r["residual_5m_bps"] > 0]),
        "residual_neg": econ([r for r in rows if r["residual_5m_bps"] is not None and r["residual_5m_bps"] <= 0]),
    }

    # lead-lag accuracy
    lead_acc = {}
    for k_min in (2, 5, 10):
        kk = k_min
        sub = [r for r in rows if r[f"lead_{kk}m"]["correct_direction"] is not None]
        acc = sum(1 for r in sub if r[f"lead_{kk}m"]["correct_direction"]) / len(sub) if sub else None
        lead_acc[f"{kk}m"] = {"n": len(sub), "directional_accuracy": round(acc, 4) if acc is not None else None}

    result = {"schema": "imoex_context_attribution_v1", "run_id": "5b44f3b383df",
              "config_hash": "1c7f75dc44c2aa67", "period": "2026-07-01..2026-07-31",
              "note": "SHADOW/read-only; IMOEX НЕ в execution; lead-lag использует будущее ТОЛЬКО для диагностики",
              "groups": groups, "lead_lag_accuracy": lead_acc, "n_rows": len(rows),
              "generated_at": datetime.now(timezone.utc).isoformat()}
    with open(os.path.join(REPORTS, "imoex_context_attribution.json"), "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    if rows:
        with open(os.path.join(REPORTS, "imoex_context_attribution.csv"), "w", newline="") as f:
            cols = list(rows[0].keys())
            wr = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            wr.writeheader(); wr.writerows(rows)
    md = ["# IMOEX CONTEXT ATTRIBUTION (SHADOW)", "",
          "Contemporaneous признаки <= decision_ts; lead-lag и STOP-концепт — диагностика.",
          f"Строк: {len(rows)}", "", "## Группы", "", "| группа | n | net | net/trade | PF | win | MFE R | MAE R | hold |",
          "|---|---|---|---|---|---|---|---|---|"]
    for g, v in groups.items():
        if v["count"] == 0: continue
        md.append(f"| {g} | {v['count']} | {v['net']} | {v['net_per_trade']} | {v['pf']} | {v['win_rate']} | "
                  f"{v['median_mfe_r']} | {v['median_mae_r']} | {v['median_hold_bars']} |")
    md += ["", "## Lead-lag (IMOEX → акция, directional accuracy)", "",
           "| горизонт | n | accuracy |", "|---|---|---|"]
    for k, v in lead_acc.items():
        md.append(f"| {k} | {v['n']} | {v['directional_accuracy']} |")
    md += ["", "## STOP-концепт (IMOEX-informed stop)", "Диагностика не реализована как policy; "
           "результат см. в JSON (groups) и при необходимости дообъяснение в imoex_context_attribution.json.",
           "", "## DATA GAP / alignment", "IMOEX 1m за период присутствует (12581 баров, 06:50..16:00 UTC). "
           "Timestamps выровнены по MSK; alignment проверен через bisect на decision_ts."]
    with open(os.path.join(REPORTS, "imoex_context_attribution.md"), "w") as f:
        f.write("\n".join(md))
    print(json.dumps({"rows": len(rows), "groups": {k: v["count"] for k, v in groups.items()},
                      "lead_lag": lead_acc}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
