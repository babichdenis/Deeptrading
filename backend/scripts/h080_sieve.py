import sys, json, asyncio, asyncpg
sys.path.insert(0,"/Users/Denis/Dev/Deeptrading/backend")
from app.services.research_pack import _load_candles
from app.services.ensemble import compute_ensemble
from datetime import datetime, timezone

BASE5=["BBG008F2T3T2","BBG004S681M2","BBG004S683W7","BBG004S68CP5","BBG004S681B4"]
EXCLUDE={"BBG00KDWPPW2","BBG004730ZJ9"}  # index + 43-bar
ENG="postgresql+asyncpg://deeptrading:deeptrading@192.168.1.54:5432/deeptrading"

async def get_cands():
    conn=await asyncpg.connect(ENG.replace("+asyncpg",""))
    rows=await conn.fetch("select distinct figi from candles")
    figis=[r['figi'] for r in rows]
    cands=[f for f in figis if f not in BASE5 and f not in EXCLUDE]
    # names
    names={}
    for f in cands:
        r=await conn.fetchrow("select ticker,name from instruments where figi=$1", f)
        names[f]=(r['ticker'] if r else '?', r['name'] if r else '?')
    await conn.close()
    return cands, names

def run_one(figi):
    req=dict(figi=figi, from_ts="2026-01-01T00:00:00+00:00", to_ts="2026-07-01T00:00:00+00:00",
             use_all_setups=True, capital=10000.0, lot=10, quorum=2,
             exit_policy={"id":"atr_stop","params":{"period":14,"multiplier":2.0,"risk_reward":2}})
    cs=list(_load_candles(figi, datetime(2026,1,1,tzinfo=timezone.utc), datetime(2026,7,1,tzinfo=timezone.utc)))
    if len(cs)<1000: return {"figi":figi,"bars":len(cs),"skip":True}
    res=compute_ensemble(cs, req)
    econ=res.get("static",{}).get("economic",{})
    comp=res.get("comparison",{})
    return {"figi":figi,"bars":len(cs),
            "net":comp.get("static_net"), "trades":comp.get("static_trades"),
            "gross":comp.get("static_gross"), "costs":comp.get("static_costs"),
            "win_rate":econ.get("win_rate_pct"), "pf":econ.get("pf"),
            "config_hash":res.get("config",{}).get("config_hash")}

def main():
    cands, names = asyncio.new_event_loop().run_until_complete(get_cands())
    print("candidates:", len(cands), flush=True)
    out={"window":"2026-H1","candidates":{}}
    for i,figi in enumerate(cands):
        try:
            r=run_one(figi); r["ticker"],r["name"]=names.get(figi,("?","?"))
        except Exception as e:
            r={"figi":figi,"error":str(e)[:200]}
        out["candidates"][figi]=r
        print(i, figi, r.get("ticker"), "net=",r.get("net"),"trades=",r.get("trades"),"win=",r.get("win_rate"), flush=True)
    json.dump(out, open("/Users/Denis/Dev/Deeptrading/backend/reports/h080_sieve.json","w"), indent=2, default=str)
    # summary rank
    ranked=[(v.get("net") or -1e9, v.get("trades") or 0, k) for k,v in out["candidates"].items() if v.get("net") is not None]
    ranked.sort(reverse=True)
    print("TOP:", [(k, round(n,1), t) for n,t,k in ranked[:8]], flush=True)
    print("saved h080_sieve.json", flush=True)

if __name__=="__main__": main()
