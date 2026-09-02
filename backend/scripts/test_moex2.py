import urllib.request, json

for interval, name in [(1, "1min"), (5, "5min"), (60, "1h")]:
    url = f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/RUAL/candles.json?from=2026-08-25&till=2026-09-01&interval={interval}"
    data = json.loads(urllib.request.urlopen(url, timeout=15).read())
    n = len(data["candles"]["data"])
    print(f"{name}: {n} candles")
