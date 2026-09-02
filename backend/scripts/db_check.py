from sqlalchemy import create_engine, text
e = create_engine("postgresql://deeptrading:deeptrading@127.0.0.1:5432/deeptrading")
with e.connect() as c:
    r = c.execute(text("SELECT column_name, data_type FROM information_schema.columns WHERE table_name='candles' ORDER BY ordinal_position"))
    for row in r: print(row)
    r2 = c.execute(text("SELECT figi, count(*) FROM candles GROUP BY figi ORDER BY count DESC LIMIT 10"))
    print("\nTOP FIGIs:")
    for row in r2: print(row)
