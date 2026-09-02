"""REGIME DIAGNOSTIC (read-only, E5 S0/S1 telemetry by bucket).

НЕ эксперимент: использует уже прогнанные S0/S1 (те же параметры E5),
строит point-in-time vol features (СТРОГО до entry_ts, без будущих баров):
  - ATR_5m (Wilder, rolling past)
  - realised vol 5m (std of past returns)
  - volume ratio (vs prior N bars)
  - session segment
  - high-vol = ATR_5m > rolling median ATR_5m предыдущих 20 торговых дней
Телеметрия: для S0 и S1 по бакетам — count, gross, costs, net, net/trade, PF,
exit_reason dist, MFE/MAE, holding_time.
Артефакт: reports/regime_diagnostic_e5_MayJune.md
"""
import sys, json, os, statistics, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from app.services.research_pack import _load_candles
from app.services.ensemble import compute_ensemble
from app.engine.models import Candle as EC

MSK = ZoneInfo('Europe/Moscow')
FIGIS = ['BBG008F2T3T2','BBG004S681M2','BBG004S683W7','BBG004S68CP5','BBG004S681B4']
T_FROM = datetime(2026,5,1,tzinfo=timezone.utc)
T_TO   = datetime(2026,7,1,tzinfo=timezone.utc)
CAPITAL = 10000.0

def base_req(figi, trailing):
    ep = {'id':'atr_stop','params':{'period':14,'multiplier':2.0,'risk_reward':2.0}}
    if trailing:
        ep['params'].update({'trail_activation_r':1.0,'trail_distance_r':1.0})
    return {'figi':figi,'bias_mode':'info','bias':{'tf':'hour','period':50},
            'entry_tf':'5min','entry':{'tf':'5min','lookback':1},'entry_session':'main',
            'quorum':2,'same_side_reentry_cooldown_bars':15,'carry_overnight':True,
            'opposite_hold':False,'exit_policy':ep,
            'commission_rate':0.0005,'slippage_bps':2.0,'capital':CAPITAL,'lot':10,
            'use_all_setups':True,'drop_useless':True,
            'from_ts':T_FROM.isoformat(),'to_ts':T_TO.isoformat()}


def atr_wilder(candles, period=14):
    out = []
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i].high, candles[i].low, candles[i-1].close
        tr = max(h-l, abs(h-pc), abs(l-pc))
        trs.append(tr)
        if len(trs) > period:
            trs.pop(0)
        if i >= period:
            out.append(sum(trs)/period)
        else:
            out.append(None)
    return [None] + out  # align index


def add_vol_features(candles_1m, trades):
    """Point-in-time vol features на entry_ts (только прошлые бары)."""
    c5 = []
    from app.services.ensemble import TF_SECONDS, resample
    c5 = resample(candles_1m, TF_SECONDS['5min'])
    atr5 = atr_wilder(c5, 14)
    closes5 = [c.close for c in c5]
    # rolling median ATR предыдущих 20 торговых дней (по дате)
    atr_by_day = {}
    for i, c in enumerate(c5):
        d = c.ts.astimezone(MSK).date().isoformat()
        v = atr5[i] if i < len(atr5) else None
        if v is not None:
            atr_by_day.setdefault(d, []).append(v)
    day_med = {d: statistics.median(vs) for d, vs in atr_by_day.items()}
    day_ord = sorted(day_med)
    # rolling median 20 пред. дней
    roll20 = {}
    for idx, d in enumerate(day_ord):
        prev = day_ord[max(0,idx-20):idx]
        roll20[d] = statistics.median([day_med[x] for x in prev]) if prev else None

    out = []
    for t in trades:
        ts = datetime.fromisoformat(t['entry_ts'].replace('Z','+00:00'))
        d = ts.astimezone(MSK).date().isoformat()
        # индекс последнего 5m бара <= entry_ts
        idx = None
        for i, c in enumerate(c5):
            if c.ts <= ts: idx = i
            else: break
        if idx is None or idx < 20:
            out.append({**t, 'atr5': None, 'hi_vol': None, 'real_vol': None, 'vol_ratio': None, 'session': None})
            continue
        atr_now = atr5[idx]
        med20 = roll20.get(d)
        hi_vol = (atr_now > med20) if (atr_now is not None and med20) else None
        # realised vol 5m: std of past 20 returns
        rets = [(closes5[i]/closes5[i-1]-1) for i in range(idx-19, idx+1) if closes5[i-1]]
        real_vol = statistics.pstdev(rets) if len(rets) > 5 else None
        # volume ratio
        vols = [c.volume for c in c5[idx-19:idx+1]]
        vol_ratio = c5[idx].volume / (sum(vols)/len(vols)) if sum(vols) else None
        # session segment
        lt = ts.astimezone(MSK)
        hm = lt.hour*60+lt.minute
        seg = 'open' if hm < 10*60+30 else ('close' if hm > 17*60 else 'mid')
        out.append({**t, 'atr5': atr_now, 'hi_vol': hi_vol, 'real_vol': real_vol,
                    'vol_ratio': vol_ratio, 'session': seg, 'day': d})
    return out


def bucket_telemetry(rows):
    """Сводка по бакетам hi_vol/session."""
    buckets = {}
    for r in rows:
        key = f"hi_vol={r.get('hi_vol')}"
        b = buckets.setdefault(key, {'count':0,'gross':0.0,'costs':0.0,'net':0.0,'wins':0,
                                     'exits':collections.Counter(),'mfe':[],'mae':[],'hold':[]})
        b['count'] += 1
        b['gross'] += r.get('gross',0)
        b['net'] += r.get('net',0)
        b['costs'] += r.get('costs',0)
        if r.get('net',0) > 0: b['wins'] += 1
        b['exits'][r.get('exit_reason')] += 1
        b['mfe'].append(r.get('mfe_r') or 0)
        b['mae'].append(r.get('mae_r') or 0)
        b['hold'].append(r.get('bars_held') or 0)
    return {k: {'count':v['count'],'gross':round(v['gross'],2),'costs':round(v['costs'],2),
                'net':round(v['net'],2),'net_per_trade':round(v['net']/max(v['count'],1),2),
                'win_rate':round(v['wins']/v['count'],4) if v['count'] else None,
                'exits':dict(v['exits']),
                'median_mfe_r':round(statistics.median(v['mfe']),3) if v['mfe'] else None,
                'median_mae_r':round(statistics.median(v['mae']),3) if v['mae'] else None,
                'median_hold':round(statistics.median(v['hold']),1) if v['hold'] else None}
            for k,v in buckets.items()}


def main():
    out_rows = []
    for arm, trailing in (('S0_baseline', False), ('S1_trailing', True)):
        for figi in FIGIS:
            candles = _load_candles(figi, T_FROM, T_TO)
            res = compute_ensemble(candles, base_req(figi, trailing))
            if 'error' in res: continue
            trades = res['static']['trades']
            rows = add_vol_features(candles, trades)
            for r in rows:
                r['arm'] = arm; r['figi'] = figi
            out_rows.extend(rows)
        print(f"{arm}: {len([r for r in out_rows if r['arm']==arm])} сделок", flush=True)

    md = ["# REGIME DIAGNOSTIC — E5 S0 vs S1 telemetry by bucket (read-only)\n",
          f"Дата: {datetime.now(timezone.utc).isoformat()}\n",
          f"Окно: 2026-05-01..2026-06-30 | FIGI: 5 | capital 10 000\n",
          "Point-in-time features (только прошлые бары до entry_ts): ATR_5m (Wilder 14),",
          "rolling median ATR 20 пред. торговых дней, realised vol 5m (std past 20 rets),",
          "volume ratio, session segment. НЕ использует будущие бары.\n"]
    for arm in ('S0_baseline','S1_trailing'):
        rows = [r for r in out_rows if r['arm']==arm]
        md.append(f"\n## {arm}\n")
        md.append(f"- сделок: {len(rows)}\n- net total: {round(sum(r['net'] for r in rows),2)}\n")
        tele = bucket_telemetry(rows)
        md.append("### По гейту high-vol (ATR>rolling med 20д)\n")
        md.append("| bucket | count | net | net/trade | win% | median MFE R | median MAE R | med hold | exits |")
        md.append("|---|---|---|---|---|---|---|---|---|")
        for k, v in tele.items():
            md.append(f"| {k} | {v['count']} | {v['net']} | {v['net_per_trade']} | {v['win_rate']} | "
                      f"{v['median_mfe_r']} | {v['median_mae_r']} | {v['median_hold']} | {v['exits']} |")
        # по session
        sess = {}
        for r in rows:
            s = r.get('session') or '?'
            sess.setdefault(s, []).append(r)
        md.append("\n### По сегменту сессии\n")
        md.append("| seg | count | net | net/trade | win% |")
        md.append("|---|---|---|---|---|")
        for s, rr in sorted(sess.items()):
            net = sum(x['net'] for x in rr)
            md.append(f"| {s} | {len(rr)} | {round(net,2)} | {round(net/len(rr),2)} | "
                      f"{round(sum(1 for x in rr if x['net']>0)/len(rr),4)} |")

    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'reports',
                       'regime_diagnostic_e5_MayJune.md')
    with open(out, 'w') as f:
        f.write('\n'.join(md))
    print('saved:', out)
    print('\n'.join(md[:20]))


if __name__ == '__main__':
    main()
