import psycopg2
conn = psycopg2.connect("host=127.0.0.1 port=5432 dbname=deeptrading user=deeptrading password=deeptrading")
cur = conn.cursor()
cur.execute("SELECT figi, ticker, lot FROM instruments WHERE ticker IN ('SBER', 'GAZP', 'LKOH', 'ROSN', 'RUAL', 'SNGSP', 'AFLT', 'MVID', 'NLMK') ORDER BY ticker")
for r in cur.fetchall():
    print("%s %s lot=%s" % (r[0], r[1], r[2]))
cur.close()
conn.close()
