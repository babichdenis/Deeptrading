import urllib.request, json

# Тикер MOEX → Figi mapping
MOEX_MAP = {
    "BBG008F2T3T2": "RUAL",
    "BBG004S681M2": "SBER",
    "BBG004S683W7": "GAZP",
    "BBG004S68CP5": "LKOH",
    "BBG004S681B4": "ROSN",
}

def fetch_moex_candles(moex_ticker, from_date, till_date, interval=1):
    """Fetch 1min candles from MOEX ISS. interval: 1=1min, 5=5min, 10=10min, 15=15min, 30=30min, 60=hour."""
    url = (
        f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR"
        f"/securities/{moex_ticker}/candles.json"
        f"?from={from_date}&till={till_date}&interval={interval}"
    )
    data = json.loads(urllib.request.urlopen(url, timeout=15).read())
    candles = data["candles"]
    columns = candles["columns"]
    col_idx = {name: i for i, name in enumerate(columns)}
    result = []
    for row in candles["data"]:
        result.append({
            "open": row[col_idx["open"]],
            "high": row[col_idx["high"]],
            "low": row[col_idx["low"]],
            "close": row[col_idx["close"]],
            "volume": row[col_idx["volume"]],
            "ts": row[col_idx["begin"]],
        })
    return result

# Test
candles = fetch_moex_candles("RUAL", "2026-09-01", "2026-09-01", interval=1)
print(f"RUAL 1min: {len(candles)} candles")
if candles:
    print(f"  First: {candles[0]}")
    print(f"  Last:  {candles[-1]}")

candles5 = fetch_moex_candles("RUAL", "2026-08-25", "2026-09-01", interval=5)
print(f"RUAL 5min (week): {len(candles5)} candles")
