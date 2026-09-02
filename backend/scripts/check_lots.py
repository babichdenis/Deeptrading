import psycopg2
conn = psycopg2.connect("host=127.0.0.1 port=5432 dbname=deeptrading user=deeptrading password=deeptrading")
cur = conn.cursor()
cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='instruments'")
print("COLS:", [r[0] for r in cur.fetchall()])
cur.execute("SELECT figi, ticker, lot FROM instruments WHERE figi IN ('BBG008F2T3T2', 'BBG004S681M2', 'BBG004S683W7', 'BBG004S68CP5', 'BBG004S681B4')")
for r in cur.fetchall():
    print("%s %s lot=%s" % (r[0], r[1], r[2]))
cur.close()
conn.close()
