"""Прогон жёстких фильтров отбора на истории сделок (walk-forward, без БД).

Фильтры:
  1. Ликвидность: реальный дневной оборот (MOEX ISS) >= max(floor, слот × adv_multiple).
  2. История тикера (walk-forward, только сделки, закрытые ДО входа): вето если
     n >= 20 и net < -50₽ и WR < 25%.
  3. (опц.) Топ-N по силе: оставить только N лучших тикеров по net истории.

Запуск: python analyze_filters.py /path/filter_dump.json [--slot 5000] [--adv 200] [--floor 300000]
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict

try:  # Windows-консоль (cp1251) не умеет ₽ — принудительно utf-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def summarize(trades: list[dict]) -> dict:
    """Полная сводка: N/Wins/Losses/Gross/Net/PF/WR/Avg/MaxDD/период."""
    n = len(trades)
    if not n:
        return {"n": 0, "wins": 0, "losses": 0, "gross_win": 0.0, "gross_loss": 0.0,
                "net": 0.0, "pf": 0.0, "wr": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                "comm": 0.0, "max_dd": 0.0, "first": None, "last": None}
    gw = sum(t["net"] for t in trades if t["net"] > 0)
    gl = sum(t["net"] for t in trades if t["net"] < 0)
    wins = sum(1 for t in trades if t["net"] > 0)
    losses = n - wins
    eq = peak = dd = 0.0
    for t in sorted(trades, key=lambda x: x.get("exit") or ""):
        eq += t["net"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"n": n, "wins": wins, "losses": losses,
            "gross_win": round(gw, 2), "gross_loss": round(gl, 2),
            "net": round(sum(t["net"] for t in trades), 2),
            "pf": round(gw / abs(gl), 2) if gl else 0.0,
            "wr": round(wins / n * 100, 1),
            "avg_win": round(gw / wins, 2) if wins else 0.0,
            "avg_loss": round(gl / losses, 2) if losses else 0.0,
            "comm": round(sum(t.get("comm") or 0 for t in trades), 2),
            "max_dd": round(dd, 2),
            "first": min((t.get("entry") or "") for t in trades),
            "last": max((t.get("exit") or t.get("entry") or "") for t in trades)}


def run(dump: dict, slot: float = 5000.0, adv_multiple: float = 200.0,
        floor: float = 300_000.0, min_n: int = 20, min_net: float = -50.0,
        max_wr: float = 0.25, top_n: int = 0, wf_top_n: int = 0,
        wf_min_hist: int = 5, wf_explore: int = 10) -> None:
    trades = sorted(dump["trades"], key=lambda t: t.get("entry") or "")
    turnover = {str(k).upper(): float(v or 0) for k, v in (dump.get("turnover") or {}).items()}
    min_turn = max(floor, slot * adv_multiple)

    hist: dict[str, list[float]] = defaultdict(list)
    kept, veto_liq, veto_hist, no_data = [], [], [], []
    for t in trades:
        tk = str(t["ticker"]).upper()
        adv = turnover.get(tk, 0.0)
        prior = hist[tk]
        n = len(prior)
        net = sum(prior)
        wr = (sum(1 for p in prior if p > 0) / n) if n else 0.0
        if 0 < adv < min_turn:
            veto_liq.append(t)
        elif n >= min_n and net < min_net and wr < max_wr:
            veto_hist.append(t)
        else:
            kept.append(t)
            if adv == 0:
                no_data.append(t)
        hist[tk].append(float(t["net"]))

    print(f"Порог ликвидности: {min_turn/1e6:.2f}M ₽/день (слот {slot:.0f}₽ × {adv_multiple:g}, floor {floor/1e6:.1f}M)")
    print(f"Вето истории: n>={min_n} И net<{min_net:.0f}₽ И WR<{max_wr*100:.0f}%\n")
    base, kp = summarize(trades), summarize(kept)
    vl, vh = summarize(veto_liq), summarize(veto_hist)
    hdr = (f"{'Набор':<26}{'Trades':>7}{'Wins':>6}{'Losses':>7}{'GrossWin':>10}"
           f"{'GrossLoss':>10}{'Net':>10}{'PF':>6}{'WR%':>6}{'AvgWin':>8}{'AvgLoss':>8}"
           f"{'MaxDD':>8}{'Comm':>8}")
    print(hdr)
    print("-" * len(hdr))
    for name, d in (("ВСЕ сделки", base), ("ПРОШЛИ фильтры", kp),
                    ("  нет данных ISS", summarize(no_data)),
                    ("Вето: неликвид", vl), ("Вето: плохая история", vh)):
        print(f"{name:<26}{d['n']:>7}{d['wins']:>6}{d['losses']:>7}{d['gross_win']:>10.1f}"
              f"{d['gross_loss']:>10.1f}{d['net']:>10.1f}{d['pf']:>6.2f}{d['wr']:>6.1f}"
              f"{d['avg_win']:>8.2f}{d['avg_loss']:>8.2f}{d['max_dd']:>8.1f}{d['comm']:>8.1f}")
    print(f"\nПериод данных: {base['first']} .. {base['last']}")

    tickers_all = sorted({str(t["ticker"]).upper() for t in trades})
    tickers_kept = sorted({str(t["ticker"]).upper() for t in kept})
    print(f"\nТикеров: было {len(tickers_all)}, прошло {len(tickers_kept)}")
    print("Прошли:", ", ".join(tickers_kept))
    lost = [t for t in tickers_all if t not in tickers_kept]
    print("Отсеяны:", ", ".join(lost) or "—")

    if top_n > 0:
        by_net: dict[str, float] = defaultdict(float)
        for t in trades:
            by_net[str(t["ticker"]).upper()] += t["net"]
        best = [tk for tk, _ in sorted(by_net.items(), key=lambda kv: -kv[1])[:top_n]]
        sel = [t for t in kept if str(t["ticker"]).upper() in best]
        s = summarize(sel)
        print(f"\nТоп-{top_n} тикеров по net истории (in-sample): {', '.join(best)}")
        print(f"{'Только топ-' + str(top_n):<26}{s['n']:>7}{s['wins']:>6}{s['losses']:>7}{s['gross_win']:>10.1f}"
              f"{s['gross_loss']:>10.1f}{s['net']:>10.1f}{s['pf']:>6.2f}{s['wr']:>6.1f}"
              f"{s['avg_win']:>8.2f}{s['avg_loss']:>8.2f}{s['max_dd']:>8.1f}{s['comm']:>8.1f}")

    if wf_top_n > 0:
        prior_net: dict[str, float] = defaultdict(float)
        prior_n: dict[str, int] = defaultdict(int)
        wf_kept, wf_skip_cold, wf_skip_rank = [], [], []
        for t in trades:
            tk = str(t["ticker"]).upper()
            ranked = [k for k, _ in sorted(prior_net.items(), key=lambda kv: -kv[1])
                      if prior_n[k] >= wf_min_hist][:wf_top_n]
            if prior_n[tk] < wf_explore:
                wf_kept.append(t)          # разведка: первые K сделок тикера торгуем
            elif prior_n[tk] < wf_min_hist:
                wf_skip_cold.append(t)
            elif tk in ranked:
                wf_kept.append(t)
            else:
                wf_skip_rank.append(t)
            prior_net[tk] += t["net"]
            prior_n[tk] += 1
        s = summarize(wf_kept)
        sc = summarize(wf_skip_cold)
        sr = summarize(wf_skip_rank)
        print(f"\nWalk-forward топ-{wf_top_n} (ранг по прошлым net, разведка {wf_explore} сделок/тикер):")
        for name, d in (("WF топ-" + str(wf_top_n), s), ("  пропуск: нет истории", sc),
                        ("  пропуск: не в топе", sr)):
            print(f"{name:<26}{d['n']:>7}{d['wins']:>6}{d['losses']:>7}{d['gross_win']:>10.1f}"
                  f"{d['gross_loss']:>10.1f}{d['net']:>10.1f}{d['pf']:>6.2f}{d['wr']:>6.1f}"
                  f"{d['avg_win']:>8.2f}{d['avg_loss']:>8.2f}{d['max_dd']:>8.1f}{d['comm']:>8.1f}")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "filter_dump.json"
    keymap = {"adv": "adv_multiple", "slot": "slot", "floor": "floor", "top_n": "top_n",
              "min_n": "min_n", "min_net": "min_net", "max_wr": "max_wr"}
    args = {}
    for a in sys.argv[2:]:
        if "=" in a:
            k, v = a.split("=", 1)
            k = keymap.get(k.lstrip("-"), k.lstrip("-"))
            args[k] = int(float(v)) if k in ("top_n", "min_n", "wf_top_n", "wf_min_hist", "wf_explore") else float(v)
    dump = json.load(open(path, encoding="utf-8"))
    run(dump, **args)
