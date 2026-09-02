import psycopg2
conn = psycopg2.connect("host=127.0.0.1 port=5432 dbname=deeptrading user=deeptrading password=deeptrading")
cur = conn.cursor()
figis = ['BBG0029SG1C1', 'TCS00A0JPP37', 'BBG000GQSRR5', 'BBG004Z2RGW8', 'BBG008F2T3T2', 'BBG004730RP0']
cur.execute("SELECT figi, ticker, lot FROM instruments WHERE figi = ANY(%s)", (figis,))
for r in cur.fetchall():
    print("%s %s lot=%s" % (r[0], r[1], r[2]))
cur.close()
conn.close()
