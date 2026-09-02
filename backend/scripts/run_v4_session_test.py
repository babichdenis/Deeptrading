#!/usr/bin/env python3
"""Тест V4 + Session Multipliers.

Сравнивает:
  A) Baseline — фиксированная позиция 10k на все сделки
  B) Session  — позиция зависит от времени дня (MOEX сессии)

Per-session статистика: win/loss количество + деньги.
"""
import sys, json, os, statistics
from datetime import datetime, timezone, timedelta, time as dtime
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.research_pack import _load_candles
from app.services.ensemble import compute_ensemble

FIGIS = ['BBG008F2T3T2','BBG004S681M2','BBG004S683W7','BBG004S68CP5','BBG004S681B4']
T_FROM = datetime(2026, 5, 1, tzinfo=timezone.utc)
T_TO   = datetime(2026, 7, 1, tzinfo=timezone.utc)
CAPITAL = 10000.0
LOT = 10
MSK = ZoneInfo("Europe/Moscow")

SESSION_BOUNDS = {
    'morning_open': (dtime(10, 0),  dtime(10, 30)),
    'morning':      (dtime(10, 30), dtime(12, 0)),
    'midday':       (dtime(12, 0),  dtime(17, 0)),
    'closing':      (dtime(17, 0),  dtime(18, 50)),
    'evening':      (dtime(19, 5),  dtime(23, 50)),
}
SESSION_MULT = {
    'morning_open': 0.7 / 1.2,
    'morning':      0.9 / 1.2,
    'midday':       1.0,
    'closing':      0.8 / 1.2,
    'evening':      0.5 / 1.2,
}
SESSION_NAMES = {
    'morning_open': '10:00-10:30',
    'morning':      '10:30-12:00',
    'midday':       '12:00-17:00',
    'closing':      '17:00-18:50',
    'evening':      '19:05-23:50',
}
SESSION_ORDER = ['morning_open', 'morning', 'midday', 'closing', 'evening']


def get_session_label(ts_utc: datetime) -> str:
    ts_msk = ts_utc.astimezone(MSK)
    t = ts_msk.time()
    for label, (start, end) in SESSION_BOUNDS.items():
        if start <= t < end:
            return label
    return 'other'


def base_req(figi):
    return {
        'figi': figi, 'bias_mode': 'info',
        'bias': {'tf': 'hour', 'period': 50},
        'entry_tf': '5min', 'entry': {'tf': '5min', 'lookback': 1},
        'entry_session': 'main',
        'quorum': 2, 'same_side_reentry_cooldown_bars': 15,
        'carry_overnight': True, 'opposite_hold': False,
        'exit_policy': {'id': 'atr_stop', 'params': {'period': 14, 'multiplier': 2.0, 'risk_reward': 2.0}},
        'commission_rate': 0.0005, 'slippage_bps': 2.0,
        'capital': CAPITAL, 'lot': LOT,
        'use_all_setups': True, 'drop_useless': True,
        'from_ts': T_FROM.isoformat(), 'to_ts': T_TO.isoformat(),
    }


def run_and_collect():
    all_trades = []
    for figi in FIGIS:
        candles = _load_candles(figi, T_FROM, T_TO)
        if not candles:
            continue
        print(f"  {figi[:12]}: {len(candles)} свечей", flush=True)
        req = base_req(figi)
        res = compute_ensemble(candles, req)
        if 'error' in res:
            print(f"  {figi[:12]}: ERROR {res['error']}", flush=True)
            continue
        stt = res['static']
        trades = stt['trades']
        for t in trades:
            entry_ts = datetime.fromisoformat(t['entry_ts'])
            session = get_session_label(entry_ts)
            mult = SESSION_MULT.get(session, 1.0)
            all_trades.append({
                'figi': figi,
                'session': session,
                'side': t['side'],
                'entry_ts': t['entry_ts'],
                'exit_ts': t['exit_ts'],
                'entry_px': t['entry_px'],
                'exit_px': t['exit_px'],
                'gross': t['gross'],
                'net': t['net'],
                'bars_held': t['bars_held'],
                'exit_reason': t['exit_reason'],
                'mult': mult,
                'net_adjusted': round(t['net'] * mult, 2),
            })
    return all_trades


def session_stats(trades, tag, use_mult=False):
    by_session = {}
    for s in SESSION_ORDER + ['other']:
        by_session[s] = {'trades': 0, 'wins': 0, 'losses': 0,
                         'win_money': 0.0, 'loss_money': 0.0, 'net': 0.0}

    for t in trades:
        s = t['session']
        if s not in by_session:
            by_session[s] = {'trades': 0, 'wins': 0, 'losses': 0,
                             'win_money': 0.0, 'loss_money': 0.0, 'net': 0.0}
        pnl = t['net_adjusted'] if use_mult else t['net']
        by_session[s]['trades'] += 1
        by_session[s]['net'] += pnl
        if pnl > 0:
            by_session[s]['wins'] += 1
            by_session[s]['win_money'] += pnl
        else:
            by_session[s]['losses'] += 1
            by_session[s]['loss_money'] += pnl

    return by_session


def print_comparison(base_by_s, sess_by_s):
    print(f"\n{'='*90}")
    print(f"{'СЕССИЯ':<16} {'БЕЗ mult':>10} {'С mult':>10} {'Δ net':>10} "
          f"{'Win (кол)':>10} {'Loose (кол)':>12} "
          f"{'Win (₽)':>10} {'Loose (₽)':>11}")
    print(f"{'='*90}")

    tot_base = 0.0
    tot_sess = 0.0
    tot_win_b = 0
    tot_win_s = 0
    tot_loss_b = 0
    tot_loss_s = 0
    tot_win_money_b = 0.0
    tot_win_money_s = 0.0
    tot_loss_money_b = 0.0
    tot_loss_money_s = 0.0

    for s in SESSION_ORDER:
        b = base_by_s.get(s, {'trades': 0, 'wins': 0, 'losses': 0,
                               'win_money': 0.0, 'loss_money': 0.0, 'net': 0.0})
        s_ = sess_by_s.get(s, {'trades': 0, 'wins': 0, 'losses': 0,
                                'win_money': 0.0, 'loss_money': 0.0, 'net': 0.0})
        delta = s_['net'] - b['net']
        name = f"{SESSION_NAMES[s]}"
        print(f"{name:<16} {b['net']:>+10.0f} {s_['net']:>+10.0f} {delta:>+10.0f} "
              f"{b['wins']:>5}/{b['trades']:<4} {s_['wins']:>5}/{s_['trades']:<4} "
              f"{b['win_money']:>+10.0f} {s_['win_money']:>+10.0f}")
        print(f"{'':>16} {'':>10} {'':>10} {'':>10} "
              f"{b['losses']:>5}/{b['trades']:<4} {s_['losses']:>5}/{s_['trades']:<4} "
              f"{b['loss_money']:>+10.0f} {s_['loss_money']:>+10.0f}")
        tot_base += b['net']
        tot_sess += s_['net']
        tot_win_b += b['wins']
        tot_win_s += s_['wins']
        tot_loss_b += b['losses']
        tot_loss_s += s_['losses']
        tot_win_money_b += b['win_money']
        tot_win_money_s += s_['win_money']
        tot_loss_money_b += b['loss_money']
        tot_loss_money_s += s_['loss_money']

    print(f"{'-'*90}")
    delta_tot = tot_sess - tot_base
    print(f"{'ИТОГО':<16} {tot_base:>+10.0f} {tot_sess:>+10.0f} {delta_tot:>+10.0f} "
          f"{tot_win_b:>5}   {tot_win_s:>5}   "
          f"{tot_win_money_b:>+10.0f} {tot_win_money_s:>+10.0f}")
    print(f"{'':>16} {'':>10} {'':>10} {'':>10} "
          f"{tot_loss_b:>5}   {tot_loss_s:>5}   "
          f"{tot_loss_money_b:>+10.0f} {tot_loss_money_s:>+10.0f}")


if __name__ == '__main__':
    cache_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'reports', 'v4_session_trades.json')

    if os.path.exists(cache_path):
        print("=== Загружаем кэш сделок ===", flush=True)
        all_trades = json.load(open(cache_path))
    else:
        print("="*70)
        print("V4 + SESSION MULTIPLIERS TEST")
        print(f"Окно: {T_FROM.date()}..{T_TO.date()}, FIGI: {len(FIGIS)}, капитал: {CAPITAL}")
        print("="*70)
        print("\n--- Сбор сделок ---", flush=True)
        all_trades = run_and_collect()
        json.dump(all_trades, open(cache_path, 'w'), indent=2, ensure_ascii=False)
        print(f"\nСохранено {len(all_trades)} сделок", flush=True)

    print(f"\nВсего сделок: {len(all_trades)}")

    base_stats = session_stats(all_trades, 'baseline', use_mult=False)
    sess_stats = session_stats(all_trades, 'session', use_mult=True)

    print_comparison(base_stats, sess_stats)

    print(f"\n{'='*90}")
    print("МНОЖИТЕЛИ:")
    for s in SESSION_ORDER:
        print(f"  {SESSION_NAMES[s]:<16} ×{SESSION_MULT[s]:.2f}  ({SESSION_MULT[s]*CAPITAL:.0f}₽)")
