import json, csv, os
def load(p):
    with open(p) as f: return json.load(f)
for name,p in [('JULY','backend/reports/5b44f3b383df/research_pack.json'),
              ('MARAPR','backend/reports/57b6244ee3eb/research_pack.json')]:
    try: d=load(p)
    except Exception as e: print(name,'ERR',e); continue
    print('===',name,'type',type(d).__name__)
    if isinstance(d,dict):
        print('top keys:', list(d.keys())[:50])
        def find(o):
            res=[]
            if isinstance(o,dict):
                for k,v in o.items():
                    if isinstance(v,list) and v and isinstance(v[0],dict) and ('net' in v[0] or 'exit_reason' in v[0] or 'entry_ts' in v[0]):
                        res.append((k,len(v),list(v[0].keys())[:30]))
                    else: res+=find(v)
            elif isinstance(o,list):
                for it in o[:30]: res+=find(it)
            return res
        for k,n,keys in find(d)[:6]:
            print('  ledger?',k,'n=',n,'keys=',keys)
    print()
g2='backend/reports/g2_daily_ohlcv.csv'
try:
    with open(g2) as f:
        r=csv.reader(f); h=next(r); print('G2 header:',h[:16]); print('G2 row1:',next(r)[:16])
except Exception as e: print('G2 ERR',e)
