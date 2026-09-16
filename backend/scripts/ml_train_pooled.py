#!/usr/bin/env python3
"""Pooled 5m direction model (no ticker_id). Train Jan 1 - Aug 14, valid Aug 15-31, OOS Sep 1-13."""
import warnings; warnings.filterwarnings('ignore')
import numpy as np, pandas as pd, psycopg2, joblib, json, os
import xgboost as xgb
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import accuracy_score, classification_report

TICKERS = ['SBER','GAZP','LKOH','ROSN','NVTK','SMLT','GMKN','RUAL']
FEE = 0.0005; RT_COST = 2*FEE; COST_BUFFER = RT_COST
TRAIN_END = pd.Timestamp('2026-08-15', tz='UTC')
VALID_END = pd.Timestamp('2026-09-01', tz='UTC')
OOS_END  = pd.Timestamp('2026-09-15', tz='UTC')

def clean_candles(df, ticker):
    n0 = len(df)
    df = df[(df['open']>0)&(df['high']>0)&(df['low']>0)&(df['close']>0)]
    df = df[(df['high']>=df['low'])&(df['volume']>=0)]
    df = df.sort_values('ts')
    pc = df['close'].shift(1)
    jump = (df['close']/pc - 1.0).abs()
    day = df['ts'].dt.tz_convert('Europe/Moscow').dt.date
    bad = jump.groupby(day).apply(lambda s:(s>0.5).sum())
    bad = bad[bad>=3].index
    df = df[~day.isin(bad)]
    pc = df['close'].shift(1)
    jump = (df['close']/pc - 1.0).abs()
    df = df[(jump<0.4)|df['ts'].eq(df['ts'].iloc[0])]
    nd = n0-len(df)
    if nd: print(f'  {ticker}: dropped {nd} corrupt ({nd/n0*100:.1f}%)')
    return df

def load_1m():
    conn = psycopg2.connect(host='127.0.0.1',port=5432,dbname='deeptrading',user='deeptrading',password='deeptrading')
    cur = conn.cursor(); out = {}
    for t in TICKERS:
        cur.execute('SELECT figi FROM instruments WHERE ticker=%s',(t,))
        row = cur.fetchone()
        if not row: continue
        cur.execute('''SELECT ts,open,high,low,close,volume FROM candles
                       WHERE figi=%s AND interval=1 AND ts>='2026-01-01' AND ts<'2026-09-15' ORDER BY ts''',(row[0],))
        rows = cur.fetchall()
        if not rows: continue
        df = pd.DataFrame(rows,columns=['ts','open','high','low','close','volume'])
        df['ts'] = pd.to_datetime(df['ts'],utc=True)
        for c in ['open','high','low','close']: df[c] = df[c].astype(float)
        df['volume'] = df['volume'].astype(float)
        df = clean_candles(df, t)
        out[t] = df
    cur.close(); conn.close(); return out

def to_5m(df1m):
    df = df1m.set_index('ts').sort_index()
    return df.resample('5min').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()

def feats_5m(df):
    f = pd.DataFrame(index=df.index)
    c, h, l, o, v = df['close'],df['high'],df['low'],df['open'],df['volume']
    for lag in [1,2,3,5,6,10,12,20,24,30,36]:
        f[f'ret_{lag}'] = c.pct_change(lag)
    f['log_ret'] = np.log(c/c.shift(1))
    lr = f['log_ret']
    for w in [6,12,24,48]:
        f[f'rvol_{w}'] = lr.rolling(w).std()
    tr = pd.concat([h-l,(h-c.shift(1)).abs(),(l-c.shift(1)).abs()],axis=1).max(axis=1)
    f['atr_6'] = tr.rolling(6).mean(); f['atr_12'] = tr.rolling(12).mean()
    f['atr_24'] = tr.rolling(24).mean(); f['atr_pct'] = f['atr_12']/c
    for w in [6,12,24,48,96]:
        sma = c.rolling(w).mean(); f[f'sma_{w}_dist'] = (c-sma)/sma
    for w in [12,26]:
        ema = c.ewm(span=w).mean(); f[f'ema_{w}_dist'] = (c-ema)/ema
    for period in [7,14,21]:
        delta = c.diff(); gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        rs = gain/loss.replace(0,1e-9); f[f'rsi_{period}'] = 100-100/(1+rs)
    e12 = c.ewm(span=12).mean(); e26 = c.ewm(span=26).mean()
    macd = e12-e26; sig = macd.ewm(span=9).mean()
    f['macd_scaled'] = macd/c; f['macd_sig'] = sig/c; f['macd_hist'] = (macd-sig)/c
    m20 = c.rolling(20).mean(); s20 = c.rolling(20).std()
    f['bb_pos'] = (c-m20)/(4*s20).replace(0,1e-9)
    f['bb_width'] = (4*s20)/m20.replace(0,1e-9)
    f['vol'] = v; f['vol_ma12'] = v.rolling(12).mean(); f['vol_ma48'] = v.rolling(48).mean()
    f['vol_ratio'] = v/f['vol_ma48'].replace(0,1e-9)
    f['vol_z'] = (v-v.rolling(48).mean())/v.rolling(48).std().replace(0,1e-9)
    hi24 = h.rolling(24).max(); lo24 = l.rolling(24).min(); rng24 = (hi24-lo24).replace(0,1e-9)
    f['pos_in_24'] = (c-lo24)/rng24
    f['hl_range'] = (h-l)/c; f['body'] = (c-o)/c
    f['upper_wick'] = (h-pd.concat([c,o],axis=1).max(axis=1))/c
    f['lower_wick'] = (pd.concat([c,o],axis=1).min(axis=1)-l)/c
    f['mom_6'] = (c-c.shift(6))/c; f['mom_12'] = (c-c.shift(12))/c
    f['dist_hi48'] = (c-h.rolling(48).max())/c; f['dist_lo48'] = (c-l.rolling(48).min())/c
    ts_msk = df.index.tz_convert('Europe/Moscow') if df.index.tz is not None else df.index.tz_localize('UTC').tz_convert('Europe/Moscow')
    hh_msk = ts_msk.hour + ts_msk.minute/60.0
    f['is_morning'] = ((hh_msk>=6.83)&(hh_msk<9.83)).astype(int)
    f['is_day'] = ((hh_msk>=9.83)&(hh_msk<19.0)).astype(int)
    f['is_evening'] = ((hh_msk>=19.0)&(hh_msk<23.83)).astype(int)
    dow = ts_msk.dayofweek
    f['dow_sin'] = np.sin(2*np.pi*dow/5); f['dow_cos'] = np.cos(2*np.pi*dow/5)
    hh_utc = df.index.hour + df.index.minute/60.0
    f['hour_sin'] = np.sin(2*np.pi*hh_utc/24); f['hour_cos'] = np.cos(2*np.pi*hh_utc/24)
    f['time_min'] = df.index.hour*1000 + df.index.minute
    return f

def main():
    print('='*70)
    print('  POOLED 5m XGB (no ticker_id) | Train Jan-Aug14, OOS Sep1-13')
    print('='*70)
    raw = load_1m()
    print(f'  Loaded {len(raw)} tickers')

    frames = []
    for t, df1m in raw.items():
        df5 = to_5m(df1m)
        feats = feats_5m(df5)
        ret5 = df5['close'].shift(-1)/df5['close'] - 1
        y3 = np.where(ret5 > COST_BUFFER, 2, np.where(ret5 < -COST_BUFFER, 0, 1))
        fr = feats.join(pd.Series(y3, index=df5.index).rename('target'))
        fr['ticker'] = t; fr['close'] = df5['close']; fr['ret5'] = ret5
        frames.append(fr)
        print(f'  {t}: {len(df5):,} 5m bars | UP={int((y3==2).sum()):5d} FLAT={int((y3==1).sum()):5d} DOWN={int((y3==0).sum()):5d}')
    data = pd.concat(frames).dropna()
    data = data[data.index < pd.Timestamp('2026-09-14 18:00', tz='UTC')]

    ft_cols = [c for c in data.columns if c not in ['target','ticker','close','ret5']]
    ft = data[ft_cols].astype(float)
    y = data['target']

    scaler = StandardScaler()
    train_mask = data.index < TRAIN_END
    valid_mask = (data.index >= TRAIN_END) & (data.index < VALID_END)
    oos_mask = data.index >= VALID_END

    scaler.fit(ft[train_mask])
    X_tr = scaler.transform(ft[train_mask]); y_tr = y[train_mask]
    X_va = scaler.transform(ft[valid_mask]); y_va = y[valid_mask]
    X_te = scaler.transform(ft[oos_mask]); y_te = y[oos_mask]
    test_data = data[oos_mask]

    print(f'  Train: {len(X_tr):,} | Valid: {len(X_va):,} | OOS: {len(X_te):,}')
    print(f'  Features ({len(ft_cols)}): {ft_cols[:5]}...{ft_cols[-5:]}')

    model = xgb.XGBClassifier(
        n_estimators=600, max_depth=7, learning_rate=0.02,
        min_child_weight=50, subsample=0.7, colsample_bytree=0.7,
        reg_lambda=1.0, reg_alpha=0.1, objective='multi:softprob',
        num_class=3, eval_metric='mlogloss', early_stopping_rounds=50,
        random_state=42, n_jobs=-1, verbosity=0)
    model.fit(X_tr, y_tr, eval_set=[(X_va, y_va)], verbose=False)
    best_iter = getattr(model, 'best_iteration', None)
    co = model.classes_
    print(f'  XGB trained | best_iter={best_iter} | classes={co}')

    # OOS predictions
    pv = model.predict_proba(X_te)
    i_up = np.where(co == co.max())[0][0]
    i_dn = np.where(co == co.min())[0][0]
    pu = pv[:, i_up]
    pd_ = pv[:, i_dn]
    oos_close = test_data['close'].values

    # Thr sweep
    print(f'\n  OOS Thr Sweep (fees {RT_COST*100:.2f}% RT):')
    results = []
    for thr in [0.50, 0.52, 0.55, 0.58, 0.60]:
        pos = np.where(pu > thr, 1.0, np.where(pd_ > thr, -1.0, 0.0))
        entry = np.insert((pos[1:] != pos[:-1]), 0, pos[0] != 0).astype(bool)
        ret_next = np.empty_like(pos); ret_next[:-1] = oos_close[1:]/oos_close[:-1]-1; ret_next[-1] = 0.0
        cost = entry * np.abs(pos) * RT_COST
        pnl = pos * ret_next
        net = (pnl.sum() - cost.sum()) * 100
        n_trades = int((pos != 0).sum())
        n_up = int((pos > 0).sum()); n_dn = int((pos < 0).sum())
        n_entry = int(entry.sum())
        win = ((pnl - cost) > 0).sum()
        wr = win/max(n_entry,1)*100
        print(f'    thr={thr:.2f}  net={net:+.2f}%  trades={n_trades}  up={n_up}  dn={n_dn}  entries={n_entry}  WR={wr:.1f}%')
        results.append({'thr': thr, 'net': net, 'trades': n_trades, 'n_up': n_up, 'n_dn': n_dn, 'entries': n_entry, 'wr': wr})

    # Per-ticker breakdown for best thr (0.50)
    best_thr = 0.50
    print(f'\n  Per-ticker OOS (thr={best_thr}):')
    for t in TICKERS:
        mask = test_data['ticker'].values == t
        if not mask.any(): continue
        p_t = pv[mask]; pu_t = p_t[:, i_up]; pd_t = p_t[:, i_dn]
        c_t = test_data.loc[mask, 'close'].values
        pos_t = np.where(pu_t > best_thr, 1.0, np.where(pd_t > best_thr, -1.0, 0.0))
        ret_t = np.empty_like(pos_t); ret_t[:-1] = c_t[1:]/c_t[:-1]-1; ret_t[-1] = 0.0
        entry_t = np.insert((pos_t[1:] != pos_t[:-1]), 0, pos_t[0] != 0).astype(bool)
        cost_t = entry_t * np.abs(pos_t) * RT_COST
        net_t = (pos_t * ret_t).sum() - cost_t.sum()
        print(f'    {t:6s}  net={net_t*100:+7.2f}%  trades={int((pos_t!=0).sum()):5d}  entries={int(entry_t.sum()):4d}')

    # Save model + scaler + meta
    out_dir = os.path.expanduser('~/Dev/Deeptrading/backend/models/ml_xgb')
    os.makedirs(out_dir, exist_ok=True)
    joblib.dump(model, os.path.join(out_dir, 'xgb_5m_pooled.joblib'))
    joblib.dump(scaler, os.path.join(out_dir, 'scaler_5m.joblib'))
    meta = {
        'feature_cols': ft_cols,
        'threshold': best_thr,
        'tickers': TICKERS,
        'train_end': str(TRAIN_END),
        'valid_end': str(VALID_END),
        'best_iter': best_iter,
        'classes': [int(c) for c in co],
        'i_up': int(i_up), 'i_dn': int(i_dn),
    }
    with open(os.path.join(out_dir, 'meta.json'), 'w') as fp:
        json.dump(meta, fp, indent=2)
    print(f'\n  Saved: {out_dir}/')
    print(f'    xgb_5m_pooled.joblib  ({os.path.getsize(os.path.join(out_dir, "xgb_5m_pooled.joblib"))//1024}KB)')
    print(f'    scaler_5m.joblib      ({os.path.getsize(os.path.join(out_dir, "scaler_5m.joblib"))//1024}KB)')
    print(f'    meta.json')
    print('Done.')

if __name__ == "__main__":
    main()
