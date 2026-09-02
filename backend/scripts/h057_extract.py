"""H-057 ML meta-model — ЧАСТЬ 1: извлечение признаков + метки для сделок canonical ensemble.

Запуск: scripts/h057_extract.py <FIGI>
Для каждого окна (2025-H1/2025-H2/2026-H1) считает compute_ensemble, извлекает признаки
на last-closed bar перед entry (БЕЗ lookahead), метку = net>0. Пишет reports/_h057_feat_{sym}.json.

Признаки:
 - vote-strength 7 функций (векторизованные приближения engine-стратегий): rsi_reversal, bollinger_reclaim,
   pullback_ema, vwap_reclaim, range_compression_breakout, macd_cross, donchian_breakout  (+ Vortex, ADX-trend как "7+")
 - quorum votes (из engine quorum_list) + reason one-hot
 - индикаторы: RSI, MACD-hist, ATR%, ADX, Vortex-diff, N-ATR
 - regime: vol-band (high/low, INSIGHT-002), FD-phase (ADX>25)
 - macro: IMOEX/GOLD/BRENT/USDRUB 1d-return
Метка: profitable = float(net) > 0
"""
import sys, json, math, asyncio
from bisect import bisect_right
from datetime import datetime, timezone, timedelta
sys.path.insert(0, "/Users/Denis/Dev/Deeptrading/backend")

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from app.services.ensemble import compute_ensemble
from app.services.research_pack import _load_candles
from app.config import get_settings

BASE = dict(bias_mode="info", bias=dict(tf="hour", period=50), entry_tf="5min",
    entry=dict(tf="5min", lookback=1), entry_session="main", quorum=2,
    same_side_reentry_cooldown_bars=15, carry_overnight=True, opposite_hold=False,
    exit_policy=dict(id="atr_stop", params=dict(period=14, multiplier=2.0, risk_reward=2.0)),
    commission_rate=0.0005, slippage_bps=2.0, capital=10000.0, lot=10,
    use_all_setups=True, drop_useless=True)

def _months():
    ms=[]; y,mo=2025,1
    while (y,mo) <= (2026,6):
        a=datetime(y,mo,1,tzinfo=timezone.utc)
        b=datetime(y+1,1,1,tzinfo=timezone.utc) if mo==12 else datetime(y,mo+1,1,tzinfo=timezone.utc)
        ms.append((f"{y}-{mo:02d}", a, b))
        if mo==12: y,mo=y+1,1
        else: mo+=1
    return ms
def _label(wn):
    y,m=wn.split("-"); m=int(m)
    if y=="2025" and m<=6: return "2025-H1"
    if y=="2025": return "2025-H2"
    return "2026-H1"
WINDOWS = _months()

SETUP_IDS = ["rsi_reversal","bollinger_reclaim","pullback_ema","vwap_reclaim",
             "range_compression_breakout","macd_cross","donchian_breakout"]

# ---------- индикаторы ----------
def sma(a, n):
    out = np.full(len(a), np.nan)
    c = np.cumsum(np.nan_to_num(a, 0.0))
    for i in range(n-1, len(a)):
        seg = a[i-n+1:i+1]
        if not np.any(np.isnan(seg)):
            out[i] = seg.mean()
    return out

def ema_arr(a, n):
    out = np.full(len(a), np.nan)
    k = 2/(n+1)
    prev = a[0]
    for i in range(1, len(a)):
        prev = a[i]*k + prev*(1-k)
        out[i] = prev
    return out

def rsi_arr(c, n=14):
    c = np.asarray(c, float)
    d = np.diff(c)
    up = np.where(d>0, d, 0.0); dn = np.where(d<0, -d, 0.0)
    g = ema_arr(up, n); l = ema_arr(dn, n)
    rs = np.where(l>0, g/l, np.nan)
    out = np.full(len(c), np.nan)
    out[1:] = 100 - 100/(1+rs)
    return out

def macd_hist(c, f=12, s=26, sig=9):
    ef = ema_arr(c, f); es = ema_arr(c, s)
    m = ef - es
    ms = ema_arr(m, sig)
    return m - ms

def atr_pct(h, l, c, n=14):
    pc = np.roll(c, 1); pc[0]=c[0]
    tr = np.maximum.reduce([np.abs(h-pc), np.abs(l-pc), h-l])
    return ema_arr(tr, n) / c * 100.0

def adx_arr(h, l, c, n=14):
    pc = np.roll(c,1); pc[0]=c[0]
    tr = np.maximum.reduce([np.abs(h-pc), np.abs(l-pc), h-l])
    up = h - np.roll(h,1); dn = np.roll(l,1) - l
    pp = np.where((up>dn)&(up>0), up, 0.0); pm = np.where((dn>up)&(dn>0), dn, 0.0)
    dx = np.where(tr>0, np.abs(pp-pm)/tr*100, 0.0)
    return ema_arr(dx, n)

def vortex(h, l, c, n=14):
    pc = np.roll(c,1); pc[0]=c[0]
    tr = np.maximum.reduce([np.abs(h-pc), np.abs(l-pc), h-l])
    up = np.abs(h - np.roll(l,1)); dn = np.abs(l - np.roll(h,1))
    vip = ema_arr(up, n)/ema_arr(tr, n)
    vim = ema_arr(dn, n)/ema_arr(tr, n)
    return vip, vim

def natr(h, l, c, n=14):
    return atr_pct(h, l, c, n)  # N-ATR ~ ATR% (INSIGHT-002 vol-фича)

# ---------- 7 векторизованных функций (signal +1/-1/0) ----------
def f_rsi_reversal(c):
    r = rsi_arr(c); s = np.zeros(len(c))
    s[r<35] = 1; s[r>65] = -1
    return s

def f_macd_cross(c):
    m = macd_hist(c); s = np.zeros(len(c))
    for i in range(1, len(c)):
        if m[i-1]<=0<m[i]: s[i]=1
        elif m[i-1]>=0>m[i]: s[i]=-1
    return s

def f_donchian(h, l, p=20):
    s = np.zeros(len(h))
    for i in range(p, len(h)):
        hi = np.max(h[i-p:i]); lo = np.min(l[i-p:i])
        if h[i] > hi: s[i]=1
        elif l[i] < lo: s[i]=-1
    return s

def f_vwap_reclaim(h, l, c, v):
    e = ema_arr(c, 20)
    s = np.zeros(len(c))
    s[c > e] = 1; s[c < e] = -1
    return s

def f_bollinger_reclaim(c, n=20, k=2):
    m = sma(c, n); sd = np.full(len(c), np.nan)
    for i in range(n-1, len(c)):
        sd[i] = np.std(c[i-n+1:i+1])
    lo = m - k*sd; hi = m + k*sd
    s = np.zeros(len(c))
    for i in range(1, len(c)):
        if c[i] > lo[i] and c[i-1] <= lo[i-1]: s[i]=1
        elif c[i] < hi[i] and c[i-1] >= hi[i-1]: s[i]=-1
    return s

def f_pullback_ema(c, n=20):
    e = ema_arr(c, n); s = np.zeros(len(c))
    s[c > e] = 1; s[c < e] = -1
    return s

def f_squeeze(h, l, c, n=20):
    m = sma(c, n); sd = np.full(len(c), np.nan)
    for i in range(n-1, len(c)):
        sd[i] = np.std(c[i-n+1:i+1])
    width = 4*sd/m
    wmed = np.nanmedian(width)
    squeezed = width < 0.6*wmed
    s = np.zeros(len(c))
    for i in range(1, len(c)):
        if squeezed[i]:
            if c[i] > m[i] + 2*sd[i]: s[i]=1
            elif c[i] < m[i] - 2*sd[i]: s[i]=-1
    return s

def f_vortex_sig(h, l, c, n=14):
    vip, vim = vortex(h, l, c, n)
    s = np.zeros(len(c))
    s[vip > vim] = 1; s[vim > vip] = -1
    return s

def f_adx_trend(h, l, c, n=14):
    ad = adx_arr(h, l, c, n)
    em = ema_arr(c, 50)
    s = np.zeros(len(c))
    s[(ad>25)&(c>em)] = 1
    s[(ad>25)&(c<em)] = -1
    return s

# ---------- macro ----------
def load_macro(eng_url):
    """Возвращает dict name->list[(ts, close)] для IMOEX(BД) и GOLD/BRENT/USDRUB(CSV)."""
    out = {}
    async def _db():
        e = create_async_engine(eng_url)
        try:
            async with e.connect() as c:
                r = await c.execute(text("SELECT ts, close FROM candles WHERE figi=:f AND interval=1 ORDER BY ts"), {"f":"BBG00KDWPPW2"})
                rows = r.fetchall()
                return [(x[0].replace(tzinfo=timezone.utc), float(x[1])) for x in rows]
        finally:
            await e.dispose()
    try:
        out["IMOEX"] = _db_async(_db)
    except Exception as ex:
        print("  IMOEX load fail:", ex); out["IMOEX"] = []
    import os, csv
    for name, fn in (("GOLD","gold_5m.csv"),("BRENT","brent_5m.csv"),("USDRUB","usdrub_5m.csv")):
        p = f"/Users/Denis/Dev/Deeptrading/backend/data/{fn}"
        if os.path.exists(p):
            try:
                ts=[]; cl=[]
                with open(p) as f:
                    for row in csv.reader(f):
                        if len(row)>=2:
                            try:
                                ts.append(datetime.fromisoformat(row[0].replace("Z","+00:00"))); cl.append(float(row[1]))
                            except: pass
                out[name] = list(zip(ts, cl))
            except Exception as ex:
                print("  CSV", name, "fail:", ex); out[name]=[]
        else:
            out[name] = []
    return out

def _db_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro())

def macro_ret(macro, name, dt, days=1):
    arr = macro.get(name, [])
    if not arr: return np.nan
    ts = [x[0] for x in arr]; cl = np.array([x[1] for x in arr])
    idx = bisect_right(ts, dt - timedelta(days=days)) - 1
    j = bisect_right(ts, dt) - 1
    if idx < 0 or j < 0 or idx == j: return np.nan
    if cl[idx] == 0: return np.nan
    return cl[j]/cl[idx] - 1.0

def main():
    figi = sys.argv[1]
    sym = {"BBG008F2T3T2":"RUAL","BBG004S681M2":"SNGP","BBG004S683W7":"AFLT",
           "BBG004S68CP5":"MVID","BBG004S681B4":"NLMK"}[figi]
    eng_url = get_settings().database_url
    macro = load_macro(eng_url)
    print(f"macro keys: {list(macro.keys())} sizes: {{(k):len(v) for k,v in macro.items()}}", flush=True)
    rows = []
    for wname, wa, wb in WINDOWS:
        req = dict(BASE); req["figi"]=figi; req["from_ts"]=wa.isoformat(); req["to_ts"]=wb.isoformat()
        candles = _load_candles(figi, wa, wb)
        if len(candles) < 100:
            print(f"  {sym} {wname}: skip, too few candles {len(candles)}"); continue
        r = compute_ensemble(candles, req)
        trades = r.get("static",{}).get("trades",[])
        entries = r.get("static",{}).get("entries",[])
        qlist = r.get("static",{}).get("quorum_list",[])
        # индексы entries/qlist по ts
        en_by_ts = {}
        for e in entries:
            en_by_ts[e["ts"]] = e
        qv_by_id = {}
        for q in qlist:
            qv_by_id[q["event_id"]] = q.get("votes")
        # свечи -> массивы
        ts = [x.ts for x in candles]
        o = np.array([x.open for x in candles], float)
        h = np.array([x.high for x in candles], float)
        l = np.array([x.low for x in candles], float)
        cl = np.array([x.close for x in candles], float)
        v = np.array([getattr(x,'volume',0.0) or 0.0 for x in candles], float)
        # per-bar signals
        sig = {
            "rsi_reversal": f_rsi_reversal(cl),
            "bollinger_reclaim": f_bollinger_reclaim(cl),
            "pullback_ema": f_pullback_ema(cl),
            "vwap_reclaim": f_vwap_reclaim(h,l,cl,v),
            "range_compression_breakout": f_squeeze(h,l,cl),
            "macd_cross": f_macd_cross(cl),
            "donchian_breakout": f_donchian(h,l),
            "vortex": f_vortex_sig(h,l,cl),
            "adx_trend": f_adx_trend(h,l,cl),
        }
        # индикаторы (вещественные)
        rsi_v = rsi_arr(cl); mh = macd_hist(cl); atr_v = atr_pct(h,l,cl); adx_v = adx_arr(h,l,cl)
        vip, vim = vortex(h,l,cl); natr_v = natr(h,l,cl)
        vol_med = np.nanmedian(atr_v)
        for t in trades:
            net = float(t["net"])
            dt = datetime.fromisoformat(t["entry_ts"].replace("Z","+00:00"))
            # last-closed bar: ближайший бар ts <= dt - 5min
            j = bisect_right(ts, dt - timedelta(minutes=5)) - 1
            if j < 14: 
                continue
            side = 1 if t["side"]=="BUY" else -1
            feats = {}
            for sid in SETUP_IDS + ["vortex","adx_trend"]:
                sv = int(sig[sid][j])
                feats[f"vs_{sid}"] = sv
                feats[f"vote_{sid}"] = 1 if sv == side else (-1 if sv == -side else 0)
            # quorum votes (из engine quorum_list)
            en = en_by_ts.get(t["entry_ts"])
            votes = qv_by_id.get(en["quorum_event_id"]) if en else None
            feats["quorum_votes"] = votes if votes is not None else -1
            feats["quorum_flag"] = 1 if (votes or 0) >= 2 else 0
            # индикаторы
            feats["rsi_val"] = float(rsi_v[j]) if not np.isnan(rsi_v[j]) else np.nan
            feats["macd_hist"] = float(mh[j]) if not np.isnan(mh[j]) else np.nan
            feats["atr_pct"] = float(atr_v[j]) if not np.isnan(atr_v[j]) else np.nan
            feats["adx_val"] = float(adx_v[j]) if not np.isnan(adx_v[j]) else np.nan
            feats["vortex_diff"] = float(vip[j]-vim[j]) if not np.isnan(vip[j]) else np.nan
            feats["natr"] = float(natr_v[j]) if not np.isnan(natr_v[j]) else np.nan
            # regime
            feats["vol_band_high"] = 1 if (not np.isnan(atr_v[j]) and atr_v[j] > vol_med) else 0
            feats["fd_trend"] = 1 if (not np.isnan(adx_v[j]) and adx_v[j] > 25) else 0
            # macro 1d returns
            for mname in ("IMOEX","GOLD","BRENT","USDRUB"):
                feats[f"macro_{mname}"] = macro_ret(macro, mname, dt, 1)
            feats["window"] = _label(wname)
            feats["profitable"] = 1 if net > 0 else 0
            feats["net"] = net
            feats["side"] = side
            rows.append(feats)
        print(f"  {sym} {wname}: trades={len(trades)} feats={len(rows)}", flush=True)
    with open(f"reports/_h057_feat_{sym}.json","w") as f:
        json.dump(rows, f)
    print(f"WROTE reports/_h057_feat_{sym}.json rows={len(rows)}", flush=True)

if __name__ == "__main__":
    main()
