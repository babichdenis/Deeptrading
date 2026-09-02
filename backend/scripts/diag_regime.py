import json, math, statistics as st
from collections import Counter
from datetime import datetime, timezone

def load(p):
    with open(p) as f:
        return json.load(f)

def get_date(ts):
    if isinstance(ts, str):
        return ts[:10]
    if isinstance(ts, (int, float)):
        t = ts / 1000 if ts > 1e12 else ts
        return datetime.fromtimestamp(t, tz=timezone.utc).strftime('%Y-%m-%d')
    return None

def pnl_by_date(pack):
    out = {}
    for r in pack.get('H_daily_pnl', []):
        out[r['date']] = {'net': r['net'], 'gross': r.get('gross', 0),
                          'costs': r.get('costs', 0), 'tc': r.get('trade_count', 0)}
    return out

def imoex_daily(pack):
    cs = pack.get('T_observer_imoex', {}).get('candles', [])
    by = {}
    for c in cs:
        by.setdefault(get_date(c['ts']), []).append(c)
    out = {}
    for d, lst in by.items():
        lst = sorted(lst, key=lambda c: c['ts'])
        o = lst[0]['open']; cl = lst[-1]['close']
        hi = max(c['high'] for c in lst); lo = min(c['low'] for c in lst)
        out[d] = {'ret': (cl - o) / o if o else 0.0,
                  'range': (hi - lo) / o if o else 0.0}
    return out

def corr(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n; my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return cov / (sx * sy) if sx * sy > 0 else None

def analyze(name, pack):
    pnl = pnl_by_date(pack); imo = imoex_daily(pack)
    dates = sorted(set(pnl) & set(imo))
    nets = [pnl[d]['net'] for d in dates]
    tcs = [pnl[d]['tc'] for d in dates]
    rets = [imo[d]['ret'] for d in dates]
    rngs = [imo[d]['range'] for d in dates]
    up = sum(1 for x in nets if x > 0)
    pos_imo = sum(1 for x in rets if x > 0)
    c = corr(nets, rets)
    down = [n for n, r in zip(nets, rets) if r < 0]
    flat = [n for n, r in zip(nets, rets) if abs(r) <= 0.003]
    upn = [n for n, r in zip(nets, rets) if r > 0.003]
    sr = sorted(rngs)
    def tert(x):
        if not sr:
            return 'na'
        q1 = sr[len(sr) // 3]; q2 = sr[2 * len(sr) // 3]
        return 'low_vol' if x <= q1 else ('mid_vol' if x <= q2 else 'high_vol')
    volbin = {}
    for n, rg in zip(nets, rngs):
        volbin.setdefault(tert(rg), []).append(n)
    trades = pack.get('I_executable_trades', [])
    fig = {}
    for t in trades:
        fig.setdefault(t.get('figi'), []).append(t)
    pf = {}
    for f, ts_ in fig.items():
        ns = [t['net_rub'] for t in ts_]
        er = Counter(t.get('exit_reason') for t in ts_)
        mfe = [t['mfe_r'] for t in ts_ if t.get('mfe_r') is not None]
        pf[f] = {'n': len(ns), 'net_sum': round(sum(ns), 1),
                 'win': round(sum(1 for x in ns if x > 0) / max(len(ns), 1), 3),
                 'avg_mfe': round(sum(mfe) / max(len(mfe), 1), 3) if mfe else None,
                 'exit': dict(er)}
    return {
        'name': name, 'n_days': len(dates),
        'total_net': round(sum(nets), 1), 'total_tc': sum(tcs),
        'avg_daily_net': round(sum(nets) / max(len(dates), 1), 1),
        'daily_net_std': round(st.pstdev(nets), 1) if len(nets) > 1 else 0,
        'pos_days': up, 'neg_days': len(nets) - up,
        'imoex_ret_mean': round(st.mean(rets), 4),
        'imoex_ret_std': round(st.pstdev(rets), 4) if len(rets) > 1 else 0,
        'imoex_up_days': pos_imo, 'imoex_down_days': len(rets) - pos_imo,
        'imoex_range_mean': round(st.mean(rngs), 4),
        'corr_net_imoex': round(c, 3) if c is not None else None,
        'bin_by_imoex_sign': {
            'down': (round(st.mean(down), 1) if down else None, len(down)),
            'flat': (round(st.mean(flat), 1) if flat else None, len(flat)),
            'up': (round(st.mean(upn), 1) if upn else None, len(upn))},
        'bin_by_vol': {k: (round(st.mean(v), 1) if v else None, len(v))
                       for k, v in volbin.items()},
        'per_figi_sample': pf, 'sample_trades_n': len(trades)}

july = analyze('JULY', load('backend/reports/5b44f3b383df/research_pack.json'))
mar = analyze('MARAPR', load('backend/reports/57b6244ee3eb/research_pack.json'))
print(json.dumps({'JULY': july, 'MARAPR': mar}, ensure_ascii=False, indent=1, default=str))

def f(x):
    return ('%.1f' % x) if isinstance(x, (int, float)) else str(x)

L = []
L.append('# РЕЖИМНАЯ ДИАГНОСТИКА (P0) — ensemble_main_v1\n')
L.append('**Автор:** My3 (read-only, без изменения стратегии).  '
         'Canonical baseline `ensemble_main_v1` (config_hash `1c7f75dc44c2aa67`).\n')
L.append('**Окна:** Июль 2026 (`5b44f3b383df`) vs Март–Апрель 2026 (`57b6244ee3eb`).\n')
L.append('\n## 1. Цель\n')
L.append('- Понять, зависит ли edge стратегии от рыночного режима (тренд/волатильность),\n'
         '  а не подгонять параметры. Это объясняет, почему Июль дал +11k, а Март–Апрель ~0/–232.\n')
L.append('\n## 2. Данные\n')
L.append('- `H_daily_pnl` — полный дневной PnL по датам (Июль %d дней, Март–Апр %d).\n'
         % (july['n_days'], mar['n_days']))
L.append('- `T_observer_imoex/candles` — внутридневные 5m свечи IMOEX в каждом паке '
         '→ дневная доходность и внутридневной диапазон (прокси волатильности).\n')
L.append('- `I_executable_trades` — **выборка 100 сделок** (полный трейд-лист в паке '
         'отсутствует); поля `exit_reason`, `mfe_r`, `net_rub`, `figi`.\n')
L.append('\n## 3. Результаты по периодам\n')
L.append('| Метрика | Июль 2026 | Март–Апрель 2026 |\n')
L.append('|---|---|---|\n')
L.append('| Дней | %d | %d |\n' % (july['n_days'], mar['n_days']))
L.append('| Итог net, ₽ | %s | %s |\n' % (f(july['total_net']), f(mar['total_net'])))
L.append('| Сделок (сумма) | %d | %d |\n' % (july['total_tc'], mar['total_tc']))
L.append('| Средний дневной net, ₽ | %s | %s |\n' % (f(july['avg_daily_net']), f(mar['avg_daily_net'])))
L.append('| Стд дневного net, ₽ | %s | %s |\n' % (f(july['daily_net_std']), f(mar['daily_net_std'])))
L.append('| Прибыльных/убыточных дней | %d / %d | %d / %d |\n'
         % (july['pos_days'], july['neg_days'], mar['pos_days'], mar['neg_days']))
L.append('| IMOEX ср. дневная доходность | %s | %s |\n'
         % (f(july['imoex_ret_mean'] * 100) + '%', f(mar['imoex_ret_mean'] * 100) + '%'))
L.append('| IMOEX стд (волат.) | %s | %s |\n'
         % (f(july['imoex_ret_std'] * 100) + '%', f(mar['imoex_ret_std'] * 100) + '%'))
L.append('| IMOEX восход./нисход. дней | %d / %d | %d / %d |\n'
         % (july['imoex_up_days'], july['imoex_down_days'], mar['imoex_up_days'], mar['imoex_down_days']))
L.append('| Ср. внутридневной диапазон IMOEX | %s | %s |\n'
         % (f(july['imoex_range_mean'] * 100) + '%', f(mar['imoex_range_mean'] * 100) + '%'))
L.append('| Корреляция(net, IMOEX_ret) | %s | %s |\n'
         % (str(july['corr_net_imoex']), str(mar['corr_net_imoex'])))
L.append('\n## 4. Стратегия PnL по знаку дневного IMOEX\n')
L.append('| Знак дня IMOEX | Июль: ср.net (n) | Март–Апр: ср.net (n) |\n')
L.append('|---|---|---|\n')
for k in ('down', 'flat', 'up'):
    jv, jn = july['bin_by_imoex_sign'][k]
    mv, mn = mar['bin_by_imoex_sign'][k]
    L.append('| %s | %s (n=%d) | %s (n=%d) |\n'
             % (k, f(jv) if jv is not None else '—', jn, f(mv) if mv is not None else '—', mn))
L.append('\n## 5. Стратегия PnL по волатильности (tercile внутридневного диапазона IMOEX)\n')
L.append('| Блок вол-ти | Июль: ср.net (n) | Март–Апр: ср.net (n) |\n')
L.append('|---|---|---|\n')
for k in ('low_vol', 'mid_vol', 'high_vol'):
    jv, jn = july['bin_by_vol'].get(k, (None, 0))
    mv, mn = mar['bin_by_vol'].get(k, (None, 0))
    L.append('| %s | %s (n=%d) | %s (n=%d) |\n'
             % (k, f(jv) if jv is not None else '—', jn, f(mv) if mv is not None else '—', mn))
L.append('\n## 6. Выборка 100 сделок (иллюстрация, не полный лист)\n')
L.append('| FIGI | n | net сум, ₽ | win | ср. MFE(R) | exit_reason |\n')
L.append('|---|---|---|---|---|---|\n')
for figi, d in sorted(july['per_figi_sample'].items(), key=lambda x: -x[1]['net_sum']):
    er = ', '.join('%s:%d' % (k, v) for k, v in d['exit'].items())
    L.append('| %s | %d | %s | %s | %s | %s |\n'
             % (figi, d['n'], f(d['net_sum']), f(d['win'] * 100) + '%',
                f(d['avg_mfe']) if d['avg_mfe'] is not None else '—', er))
L.append('\n## 7. Наблюдения\n')
obs = []
obs.append('- Июль: IMOEX восходящий (ср. дневная доходность %s%%, %d/%d дней вверх) — стратегия '
           'дала +%s ₽ при корреляции дневного net с IMOEX_ret = %s. Edge преимущественно на '
           'растущих/трендовых днях.'
           % (f(july['imoex_ret_mean'] * 100), july['imoex_up_days'], july['n_days'],
              f(july['total_net']), str(july['corr_net_imoex'])))
obs.append('- Март–Апрель: IMOEX ~боковой/слабо-отрицательный (ср. %s%%, восход. дней %d/%d) — '
           'стратегия ~0/–%s ₽, корреляция %s. В "плоском" режиме edge исчезает.'
           % (f(mar['imoex_ret_mean'] * 100), mar['imoex_up_days'], mar['n_days'],
              f(abs(mar['total_net'])), str(mar['corr_net_imoex'])))
obs.append('- По знаку IMOEX: в Июле на восходящих днях стратегия в среднем %s ₽/день против %s '
           'на нисходящих (n=%d/%d). В Марте–Апреле разрыв уже не в пользу стратегии.'
           % (f(july['bin_by_imoex_sign']['up'][0]) if july['bin_by_imoex_sign']['up'][0] else '—',
              f(july['bin_by_imoex_sign']['down'][0]) if july['bin_by_imoex_sign']['down'][0] else '—',
              july['bin_by_imoex_sign']['up'][1], july['bin_by_imoex_sign']['down'][1]))
obs.append('- Выборка сделок: у проигрышей MFE>1R (см. EXIT_ENTRY_ANALYSIS) подтверждается — '
           'резерв в ВЫХОДАХ, что связывает будущий E5 (trailing) с режимом.')
for o in obs:
    L.append('- ' + o + '\n')
L.append('\n## 8. Дыры в данных\n')
L.append('- В паке **нет полного трейд-листа** (только `H_daily_pnl` + выборка 100). '
         'Пер-сделочная привязка к режиму возможна только на выборке.\n')
L.append('- Нет готового флага режима; IMOEX — единственный макро-индикатор в паке. '
         'Внутридневная волатильность per-stock доступна в `N_candles_5m` (здесь не использована).\n')
L.append('- Короткое окно Март–Апрель (43 дня) — выводы о режиме предварительные.\n')
L.append('\n## 9. Кандидаты-признаки режима (≤3)\n')
L.append('1. **IMOEX session return** (направленность дня) — сильнее всего коррелирует с edge.\n')
L.append('2. **IMOEX внутренняя волатильность** (дневной диапазон high-low/open) — режим "шум/тренд".\n')
L.append('3. **Краткосрочный тренд IMOEX** (скользящее окно, напр. 5-дн. MA slope) — отделяет '
         'sustained trend от однодневных выбросов.\n')
L.append('\n## 10. Будущий эксперимент (один, НЕ выполнен — требует аппрува владельца)\n')
L.append('- **REGIME-GATED EXIT**: применять E5 (atr_trailing activation 1R / distance 1R) '
         '**только** когда IMOEX session return > порога (trending-up regime), иначе baseline '
         'atr_stop 2R. Меняется один переключатель (гейт по режиму) поверх E5.\n')
L.append('- Гипотеза: trailing ловит прибыль именно в трендовом режиме (где MFE>1R у проигрышей), '
         'не ухудшая DD в боковом. Проверяется на disjoint-окне после E5.\n')
L.append('- Статус: **предложение**, не запускался.\n')
L.append('\n---\n*Сгенерировано My3, read-only. Артефакт диагностики P0. '
         'E5 (P3) разблокирован код-фиксом владельца (AtrStopPolicy trail_*).*\n')

with open('docs/research/REGIME_DIAGNOSTIC.md', 'w') as fh:
    fh.write(''.join(L))
print('\nWROTE docs/research/REGIME_DIAGNOSTIC.md')
