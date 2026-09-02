#!/usr/bin/env python3
"""Debug: check MOEX ISS IS_TRADED format."""
import json, urllib.request
url = "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json?iss.meta=off&iss.only=securities"
data = json.loads(urllib.request.urlopen(url, timeout=30).read())
cols = data["securities"]["columns"]
rows = data["securities"]["data"]
print("columns:", cols)
print("total rows:", len(rows))
if rows:
    d = dict(zip(cols, rows[0]))
    print("first row:", d)
    for k in ["IS_TRADED", "LOTSIZE", "SHORTNAME"]:
        print(f"  {k}: type={type(d.get(k))} val={d.get(k)}")
    # Count traded
    traded = sum(1 for r in rows if dict(zip(cols, r)).get("IS_TRADED") == 1)
    print(f"IS_TRADED == 1: {traded}")
    # Check unique values
    vals = set(dict(zip(cols, r)).get("IS_TRADED") for r in rows)
    print(f"unique IS_TRADED values: {vals}")
