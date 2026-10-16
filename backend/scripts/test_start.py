#!/usr/bin/env python3
"""ЕДИНЫЙ ЗАПУСК ТЕСТА ПО API: data/test_launch.json -> всё применяется и ПОКАЗЫВАЕТСЯ.

Тег-карточка (под прогрессбаром) заполняется из манифеста: список роботов,
период в МСК, кворум, выходы (SL/TP), сессии/режимы/overnight, издержки.
"""
import json, sys, time, urllib.request
from datetime import datetime, timezone, timedelta

API = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
M = json.load(open("data/test_launch.json", encoding="utf-8"))
MSK = timezone(timedelta(hours=3))

def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

def msk(s):
    if not s:
        return "—"
    d = datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(MSK)
    return d.strftime("%d.%m.%Y %H:%M МСК")

eng = M.get("engine") or {}
tr = M.get("trading") or {}
rp = M.get("replay") or {}

if eng.get("use_ensemble"):
    res = call("PATCH", "/api/v1/bot/ensemble", {
        "quorum": int(eng.get("quorum", 1)),
        "neutral_mode": eng.get("neutral_mode", "semi_flip"),
        "setups": eng.get("setups") or [],
    })
    print("ensemble:", {"quorum": res.get("config", {}).get("quorum"),
                        "setups": len(res.get("config", {}).get("setups") or [])})

cfg_patch = {}
for k_api, k_m in (("long_allowed","long_allowed"), ("short_allowed","short_allowed"),
                   ("overnight","overnight"), ("sessions","sessions"),
                   ("trade_regimes","regimes")):
    if k_m in tr:
        cfg_patch[k_api] = tr[k_m]
if cfg_patch:
    res = call("PATCH", "/api/v1/bot/config", cfg_patch)
    print("config:", {k: res.get(k) for k in cfg_patch})

ex = M.get("exits") or {}
if ex:
    ex_patch = {"atr_multiplier": float(ex.get("sl_atr", 4)),
                "atr_risk_reward": float(ex.get("risk_reward", 4))}
    for k_api, k_m in (("trail_activation_comm_mult","trail_activation_comm_mult"),
                       ("trail_distance_atr","trail_distance_r"),
                       ("trail_compress_r","trail_compress_r"),
                       ("trail_min_factor","trail_min_factor"),
                       ("trail_min_atr","trail_min_atr"),
                       ("trail_vol_boost","trail_vol_boost")):
        if k_m in ex:
            ex_patch[k_api] = float(ex[k_m])
    res = call("PATCH", "/api/v1/bot/config", ex_patch)
    print("exits:", {k: res.get(k) for k in ex_patch})

# --- тег-карточка из манифеста (то, что видно под прогрессбаром) ---
setups = eng.get("setups") or []
robots_str = "ensemble(%d): " % len(setups) + ", ".join(
    f"{s['strategy_id']} {s.get('tf','')}" for s in setups)
rr = float(eng.get("risk_reward", 4))
preset = {
    "preset": {"id": "(manifest)", "name": "Единый манифест test_launch.json"},
    "harness": {
        "period": [msk(rp.get("start")), msk(rp.get("end"))],
        "timeframe": eng.get("interval", "5min"),
        "universe": [""] * int(tr.get("universe_size", 30)),
        "costs": {"commission": "0.05%", "slippage_bps": 2},
        "exits": [f"TP ×{rr:g}R", ("trail " + str(ex.get("trail_activation_comm_mult")) + "× комиссии / отступ " + str(ex.get("trail_distance_r")) + "R") if ex.get("mode")=="trailing" else "fixed"],
    },
    "targets": {"replay": {"engine": robots_str,
                           "interval": eng.get("interval", "5min"),
                           "pace": rp.get("pace", "fast")}},
    "runtime": {
        "sessions": list(tr.get("sessions") or ["day"]),
        "regimes": list(tr.get("regimes") or []),
        "overnight": bool(tr.get("overnight", False)),
        "entry": {"quorum": int(eng.get("quorum", 1)), "confirm_flip": 0, "cooldown_bars": 0},
        "exits": {"sl_mode": "atr", "initial_sl_atr": float(ex.get("sl_atr", 4)),
                  "trail_activation_comm_mult": ex.get("trail_activation_comm_mult"),
                  "trail_distance_atr": ex.get("trail_distance_r"),
                  "trail_compress_r": ex.get("trail_compress_r"),
                  "trail_min_factor": ex.get("trail_min_factor"),
                  "trail_min_atr": ex.get("trail_min_atr"),
                  "trail_vol_boost": ex.get("trail_vol_boost")},
        "money": {"initial_cash": int(tr.get("capital", 10000)), "qty_per_trade": 1,
                  "max_positions": int(tr.get("max_positions", 20))},
        "bias": {},
    },
}

call("POST", "/api/v1/bot/stop")
time.sleep(5)
r = call("POST", f"/api/v1/bot/reset?initial_cash={int(tr.get('capital', 10000))}")
print("reset:", r)

res = call("POST", "/api/v1/bot/mode", {
    "mode": "test", "test_name": M["test_name"],
    "replay_start": rp.get("start", ""), "replay_end": rp.get("end", ""),
    "replay_pace": rp.get("pace", "fast"),
    "test_engine": eng.get("mode", "ensemble_v4"),
    "test_interval": eng.get("interval", "5min"),
    "preset": preset,
})
print("started:", {k: res.get(k) for k in ("mode", "test_name", "restarted")})
time.sleep(15)
st = call("GET", "/api/v1/bot/status")
c = st.get("config") or {}
print("applied:", {"test_name": c.get("test_name"), "engine": c.get("strategy_id"),
                   "use_ensemble": c.get("use_ensemble"), "tf": c.get("interval_name")})
tags = call("GET", f"/api/v1/bot/tests/{M['test_name']}/tags")
print("TAGS:")
for g in tags.get("groups") or []:
    print("  " + g["group"] + ": " + " · ".join(f"{i['k']}={i['v']}" for i in g["items"]))
