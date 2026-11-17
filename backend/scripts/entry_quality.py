"""B4 ENTRY-QUALITY MICRO-ANALYSIS (SHADOW, read-only).

Для каждой сделки July (5b44f3b383df) на decision_ts (без look-ahead):
  (a) цена vs EMA_20 (5m) — trend-alignment со стороной сделки;
  (b) pullback depth перед breakout (макс. adverse retrace от недавнего swing high/low до decision, в bps);
  (c) MAE_early — макс. adverse экскурсия в первые 3 бара 1m после входа (в bps).
Группы: trend-aligned vs not; shallow/medium/deep pullback (терции); low/high early-noise (медиана).
Артефакты: новый --output-dir; early MAE — post-entry диагностика.
"""
import sys, os, json, csv, statistics, bisect
import argparse
import math
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(Path(__file__).parent))
from last_closed_bar import last_closed_bar
from macro_regime_attribution_v3 import load_trades, file_hashes

REPORTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
RUN_DIR = os.path.join(REPORTS, "5b44f3b383df")
FIGIS = ["BBG008F2T3T2", "BBG004S681M2", "BBG004S683W7", "BBG004S68CP5", "BBG004S681B4"]
T_FROM = datetime(2026, 7, 1, tzinfo=timezone.utc)
T_TO = datetime(2026, 8, 1, tzinfo=timezone.utc)


def ema20_at(closes: list[float], idx: int) -> float | None:
    if idx < 19 or idx >= len(closes):
        return None
    value = closes[0]
    for close in closes[1:idx + 1]:
        value += (close - value) * 2 / 21
    return value


def timestamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.utcoffset() is None:
        raise ValueError('timezone required')
    return result.astimezone(timezone.utc)


def number(value, optional=False):
    if optional and value in (None, ''):
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('finite number required')
    return result


def validate_candles(candles):
    previous = None
    for candle in candles:
        if candle.ts.utcoffset() is None or (previous is not None and candle.ts <= previous):
            raise ValueError('candles require aware, strictly increasing timestamps')
        values = [number(getattr(candle, key)) for key in ('open', 'high', 'low', 'close')]
        opening, high, low, close = values
        if not 0 < low <= min(opening, close) <= max(opening, close) <= high:
            raise ValueError('invalid OHLC')
        previous = candle.ts


def analyze_trade(trade, c5, c1):
    side = trade['side']
    if side not in ('LONG', 'SHORT'):
        raise ValueError('side must be LONG or SHORT')
    decision = timestamp(trade['decision_time'])
    entry = timestamp(trade['entry_time'])
    exit_time = timestamp(trade['exit_time']) if trade.get('exit_time') else None
    if entry < decision or (exit_time is not None and exit_time < entry):
        raise ValueError('invalid trade chronology')
    price = number(trade['entry_price'])
    if price <= 0:
        raise ValueError('entry price must be positive')
    j5 = last_closed_bar([c.ts for c in c5], decision)
    ema = ema20_at([c.close for c in c5], j5)
    trend, pullback = None, None
    if ema is not None:
        decision_price = c5[j5].close
        trend = decision_price > ema if side == 'LONG' else decision_price < ema
        look = c5[j5-19:j5+1]
        swing = max(c.high for c in look) if side == 'LONG' else min(c.low for c in look)
        pullback = (swing-decision_price)/swing*10000 if side == 'LONG' else (decision_price-swing)/swing*10000
    e1 = bisect.bisect_left([c.ts for c in c1], entry)
    early = c1[e1:e1+3]
    mae = None
    reason = 'incomplete_window'
    expected = [entry + timedelta(minutes=i) for i in range(3)]
    if [c.ts for c in early] == expected:
        if exit_time is not None and exit_time < entry + timedelta(minutes=3):
            reason = 'exit_before_window_end'
        else:
            adverse = price-min(c.low for c in early) if side == 'LONG' else max(c.high for c in early)-price
            mae = max(0.0, adverse/price*10000)
            reason = 'known'
    return {
        'trade_id': trade['trade_id'], 'figi': trade['figi'], 'side': side,
        'decision_time': decision.isoformat(), 'entry_time': entry.isoformat(),
        'net': number(trade['net_rub']), 'gross': number(trade['gross_rub']),
        'mfe_r': number(trade.get('mfe_r'), True), 'mae_r': number(trade.get('mae_r'), True),
        'hold_bars': number(trade.get('hold_bars'), True),
        'trend_aligned': trend, 'pullback_bps': pullback, 'mae_early_bps': mae,
        'decision_features_status': 'known' if ema is not None else 'insufficient_history',
        'mae_early_status': reason,
    }


def optional_median(rows, key, digits):
    values = [row[key] for row in rows if row[key] is not None]
    return round(statistics.median(values), digits) if values else None


def econ(sub):
    if not sub:
        return {'count': 0}
    net = sum(row['net'] for row in sub)
    gains = sum(max(row['net'], 0) for row in sub)
    losses = sum(max(-row['net'], 0) for row in sub)
    return {'count': len(sub), 'net': round(net, 2), 'net_per_trade': round(net/len(sub), 2),
            'pf': round(gains/losses, 3) if losses else None,
            'win_rate': sum(row['net'] > 0 for row in sub)/len(sub),
            'median_mfe_r': optional_median(sub, 'mfe_r', 3),
            'median_mae_r': optional_median(sub, 'mae_r', 3),
            'median_hold': optional_median(sub, 'hold_bars', 1)}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args(argv)
    destination = Path(args.output_dir)
    if destination.exists():
        parser.error('--output-dir must be a new directory')
    from app.services.research_pack import _load_candles
    from app.services.ensemble import resample, TF_SECONDS
    print("loading candles ...", flush=True)
    stock_5m = {}
    stock_1m = {}
    for figi in FIGIS:
        cs = _load_candles(figi, T_FROM - timedelta(days=3), T_TO + timedelta(minutes=3))
        validate_candles(cs)
        stock_1m[figi] = cs
        stock_5m[figi] = resample(cs, TF_SECONDS["5min"])
    print("candles loaded", flush=True)

    trades = load_trades(os.path.join(RUN_DIR, 'trades.csv'))
    rows = []
    for trade in trades:
        figi = trade['figi']
        if figi in stock_5m:
            rows.append(analyze_trade(trade, stock_5m[figi], stock_1m[figi]))
    print(f'rows: {len(rows)}', flush=True)

    # терции pullback
    pbs = sorted([r["pullback_bps"] for r in rows if r["pullback_bps"] is not None])
    q1 = pbs[len(pbs) // 3] if pbs else None
    q2 = pbs[2 * len(pbs) // 3] if pbs else None
    # медиана early noise
    maes = sorted([r["mae_early_bps"] for r in rows if r["mae_early_bps"] is not None])
    med_noise = statistics.median(maes) if maes else None

    groups = {
        "trend_aligned": econ([r for r in rows if r["trend_aligned"] is True]),
        "trend_not_aligned": econ([r for r in rows if r["trend_aligned"] is False]),
        "pullback_shallow": econ([r for r in rows if r["pullback_bps"] is not None and r["pullback_bps"] <= q1]),
        "pullback_medium": econ([r for r in rows if r["pullback_bps"] is not None and q1 < r["pullback_bps"] <= q2]),
        "pullback_deep": econ([r for r in rows if r["pullback_bps"] is not None and r["pullback_bps"] > q2]),
        "early_noise_low": econ([r for r in rows if r["mae_early_bps"] is not None and r["mae_early_bps"] <= med_noise]),
        "early_noise_high": econ([r for r in rows if r["mae_early_bps"] is not None and r["mae_early_bps"] > med_noise]),
    }
    report = {"schema": "entry_quality_v2", "run_id": "5b44f3b383df", "config_hash": "1c7f75dc44c2aa67",
              "period": "2026-07-01..2026-07-31",
              "note": "EMA/pullback: closed bars at decision; early MAE: post-entry diagnostic, unavailable at decision. Thresholds fitted on this sample: descriptive only.",
              "groups": groups, "pullback_q1_bps": q1, "pullback_q2_bps": q2,
              "early_noise_median_bps": med_noise, "n_rows": len(rows),
              "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()}
    report['coverage'] = {'input_trades': len(trades), 'unmapped_trades': len(trades)-len(rows),
                          'unknown_decision_features': sum(r['trend_aligned'] is None for r in rows),
                          'unknown_early_mae': sum(r['mae_early_bps'] is None for r in rows)}
    payload = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    destination.mkdir(parents=True, exist_ok=False)
    with (destination/'entry_quality.json').open('x', encoding='utf-8') as f:
        f.write(payload)
    with (destination/'entry_quality.csv').open('x', newline='', encoding='utf-8') as f:
        fields = list(rows[0]) if rows else ['trade_id', 'figi', 'side', 'decision_time', 'entry_time', 'net', 'gross', 'mfe_r', 'mae_r', 'hold_bars', 'trend_aligned', 'pullback_bps', 'mae_early_bps', 'decision_features_status', 'mae_early_status']
        wr = csv.DictWriter(f, fieldnames=fields)
        wr.writeheader()
        wr.writerows(rows)
    md = ["# ENTRY-QUALITY MICRO-ANALYSIS (SHADOW, read-only)", "",
          "EMA/pullback по закрытым барам на decision_ts. Early MAE — диагностика после входа, не входной признак.", "",
          f"Строк: {len(rows)} | pullback Q1: {q1} bps, Q2: {q2} bps | early-noise median: {med_noise} bps", "",
          "| группа | n | net | net/trade | PF | win | MFE R | MAE R | hold |",
          "|---|---|---|---|---|---|---|---|---|"]
    for g, v in groups.items():
        if v["count"] == 0:
            continue
        md.append(f"| {g} | {v['count']} | {v['net']} | {v['net_per_trade']} | {v['pf']} | {v['win_rate']} | "
                  f"{v['median_mfe_r']} | {v['median_mae_r']} | {v['median_hold']} |")
    md += ["", "## Вывод", "НЕ предлагать entry filter без повторяемости на ≥2 независимых периодах."]
    with (destination/'entry_quality.md').open('x', encoding='utf-8') as f:
        f.write("\n".join(md))
    outputs = [destination/name for name in ('entry_quality.json', 'entry_quality.csv', 'entry_quality.md')]
    with (destination/'completion.json').open('x', encoding='utf-8') as f:
        json.dump({'status': 'complete', 'outputs': file_hashes(outputs)}, f)
    print(json.dumps({"rows": len(rows), "groups": {k: v["count"] for k, v in groups.items()},
                      "q": [q1, q2], "noise_med": med_noise},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
