#!/usr/bin/env python3
"""Эталонный тест V4: baseline (канонические параметры) vs + ML-фильтр.
5 FIGI, окно 2026-05-01..2026-07-01, капитал 10k, quorum=2.
ML-фильтр: встроенный MlEnsembleFilter (LightGBM, 2024-2025, 25 акций, 5m).
"""
import sys, json, os, statistics
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timezone
from app.services.research_pack import _load_candles
from app.services.ensemble import compute_ensemble

FIGIS = ['BBG008F2T3T2','BBG004S681M2','BBG004S683W7','BBG004S68CP5','BBG004S681B4']
T_FROM = datetime(2026,5,1,tzinfo=timezone.utc)
T_TO   = datetime(2026,7,1,tzinfo=timezone.utc)
CAPITAL = 10000.0


def base_req(figi, gate):
    return {'figi':figi,'bias_mode':'info','bias':{'tf':'hour','period':50},
            'entry_tf':'5min','entry':{'tf':'5min','lookback':1},'entry_session':'main',
            'quorum':2,'same_side_reentry_cooldown_bars':15,'carry_overnight':True,
            'opposite_hold':False,
            'exit_policy':{'id':'atr_stop','params':{'period':14,'multiplier':2.0,'risk_reward':2.0}},
            'commission_rate':0.0005,'slippage_bps':2.0,'capital':CAPITAL,'lot':10,
            'use_all_setups':True,'drop_useless':True,
            'entry_pullback_depth': gate or None,
            'from_ts':T_FROM.isoformat(),'to_ts':T_TO.isoformat()}


def run_arm(tag, ml_filter_cfg=None):
    cache_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'reports', f'v4_{tag}.json')
    if os.path.exists(cache_path):
        done = {r['figi']: r for r in json.load(open(cache_path))}
    else:
        done = {}
    rows = list(done.values())
    for figi in FIGIS:
        if figi in done:
            print(f"  {figi[:12]}: кэш net={done[figi]['net']:.0f} trades={done[figi]['trades']}", flush=True)
            continue
        candles = _load_candles(figi, T_FROM, T_TO)
        if candles:
            print(f"  WINDOW {figi[:12]}: {len(candles)} свечей 1m, {candles[0].ts.isoformat()} .. {candles[-1].ts.isoformat()}", flush=True)
        req = base_req(figi, None)
        if ml_filter_cfg:
            req['ml_filter'] = ml_filter_cfg
        res = compute_ensemble(candles, req)
        if 'error' in res:
            print(f"  {figi[:12]}: ERROR {res['error']}", flush=True)
            continue
        stt = res['static']; e = stt['economic']; trades = stt['trades']
        rejected = stt.get('rejected', []) or []
        ml_rejected = sum(1 for r in rejected if r.get('reason') == 'ML_REJECTED') if isinstance(rejected, list) else 0
        nets = [t['net'] for t in trades]
        gross = sum(t['gross'] for t in trades)
        wins = sum(1 for t in trades if t['net'] > 0)
        los = sum(1 for t in trades if t['net'] <= 0)
        gp = sum(max(t['gross'],0) for t in trades)
        gl = sum(max(-t['gross'],0) for t in trades)
        pf = gp/gl if gl > 0 else float('inf')
        cum, peak, dd = 0.0, 0.0, 0.0
        for t in trades:
            cum += t['net']; peak = max(peak, cum); dd = max(dd, peak - cum)
        r = {'figi':figi, 'trades':len(trades), 'wins':wins, 'losses':los,
             'winrate': (wins/len(trades)) if trades else 0,
             'gross': round(gross,2), 'net': round(sum(nets),2),
             'net_per_trade': round(statistics.mean(nets),3) if nets else 0,
             'pf': round(pf,3) if pf != float('inf') else None,
             'maxdd': round(dd,2), 'avg_hold_bars': round(statistics.mean(t['bars_held'] for t in trades),1) if trades else 0,
             'ml_rejected': ml_rejected}
        rows.append(r)
        done[figi] = r
        json.dump(list(done.values()), open(cache_path,'w'), indent=2)
        print(f"  {figi[:12]}: net={r['net']:.0f} trades={r['trades']} wr={r['winrate']:.1%} pf={r['pf']} dd={r['maxdd']:.0f}", flush=True)
    return rows


def summarize(rows, tag):
    if not rows:
        return
    n = len(rows)
    tot_net = sum(r['net'] for r in rows)
    tot_trades = sum(r['trades'] for r in rows)
    avg_wr = statistics.mean(r['winrate'] for r in rows)
    avg_nt = statistics.mean(r['net_per_trade'] for r in rows)
    pfs = [r['pf'] for r in rows if r['pf'] is not None]
    avg_pf = statistics.mean(pfs) if pfs else None
    tot_dd = max(r['maxdd'] for r in rows)
    wins_sum = sum(r['wins'] for r in rows)
    tot_ml = sum(r.get('ml_rejected', 0) for r in rows)
    print(f"\n=== {tag} ===")
    print(f"  Сумма net:      {tot_net:.0f} ₽")
    print(f"  Сделок:         {tot_trades}")
    print(f"  Средний WR:     {avg_wr:.1%}")
    print(f"  Net/сделка:     {avg_nt:.3f} ₽")
    print(f"  Средний PF:     {avg_pf if avg_pf else 'n/a'}")
    print(f"  Max DD:         {tot_dd:.0f} ₽")
    print(f"  Побед/поражений: {wins_sum}/{tot_trades-wins_sum}")
    if tot_ml:
        print(f"  Отклонено ML:   {tot_ml}")


if __name__ == '__main__':
    print("="*70)
    print("ЭТАЛОННЫЙ ТЕСТ V4: baseline vs ML-фильтр")
    print(f"Окно: {T_FROM.date()}..{T_TO.date()}, FIGI: {len(FIGIS)}, капитал: {CAPITAL}")
    print("="*70)

    # Baseline
    print("\n--- ARM 1: BASELINE (без фильтра) ---")
    base_rows = run_arm('baseline')
    summarize(base_rows, "BASELINE")

    # ML filter
    print("\n--- ARM 2: + ML-фильтр (LightGBM 2024-25, threshold 0.55) ---")
    ml_rows = run_arm('mlfilter', {'threshold': 0.55})
    summarize(ml_rows, "ML FILTER thr=0.55")

    # Compare
    print("\n" + "="*70)
    print("СРАВНЕНИЕ ПО ТИКЕРАМ")
    print("="*70)
    print(f"{'FIGI':<14} {'BASE net':>10} {'ML net':>10} {'BASE trades':>12} {'ML trades':>10} {'ΔWR':>8} {'ML rej':>8}")
    for b, m in zip(base_rows, ml_rows):
        dwr = (m['winrate'] - b['winrate']) * 100
        print(f"{b['figi'][:12]:<14} {b['net']:>10.0f} {m['net']:>10.0f} {b['trades']:>12} {m['trades']:>10} {dwr:>+8.1f}% {m.get('ml_rejected',0):>8}")

    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       'reports', 'v4_comparison.json')
    json.dump({'baseline': base_rows, 'ml_filter': ml_rows}, open(out,'w'), indent=2, ensure_ascii=False)
    print(f"\nСохранено: {out}")
