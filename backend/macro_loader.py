# -*- coding: utf-8 -*-
"""Load macro factors into macro_daily: IMOEX, USD, EUR, CNY, GOLD, BRENT, NG."""
import xml.etree.ElementTree as ET

import requests
import psycopg2

FROM = "2025-11-01"
DSN = "host=127.0.0.1 port=5432 dbname=deeptrading user=deeptrading"

S = requests.Session()
S.headers["User-Agent"] = "macro-loader/1.0"

UPSQL = ("INSERT INTO macro_daily (secid, ts, open, high, low, close, volume) "
         "VALUES (%s,%s,%s,%s,%s,%s,%s) "
         "ON CONFLICT (secid, ts) DO UPDATE SET open=EXCLUDED.open, high=EXCLUDED.high, "
         "low=EXCLUDED.low, close=EXCLUDED.close, volume=EXCLUDED.volume")


def moex_history(engine, market, board, secid):
    rows, start = [], 0
    while True:
        url = ("https://iss.moex.com/iss/history/engines/%s/markets/%s/boards/%s/"
               "securities/%s.json?from=%s&start=%d&iss.meta=off"
               % (engine, market, board, secid, FROM, start))
        j = S.get(url, timeout=30).json()
        cols = j["history"]["columns"]
        data = j["history"]["data"]
        if not data:
            break
        idx = {c: i for i, c in enumerate(cols)}
        vi = idx.get("VOLUME", idx.get("VALUE"))
        for r in data:
            d = r[idx["TRADEDATE"]]
            if not d:
                continue
            rows.append((secid, d, r[idx["OPEN"]], r[idx["HIGH"]], r[idx["LOW"]],
                         r[idx["CLOSE"]], r[vi]))
        start += len(data)
        if len(data) < 100 or start > 1000:
            break
    return rows


def moex_candles(secid):
    rows, start = [], 0
    while True:
        url = ("https://iss.moex.com/iss/engines/currency/markets/selt/boards/CETS/"
               "securities/%s/candles.json?interval=24&from=%s&start=%d&iss.meta=off&iss.only=candles"
               % (secid, FROM, start))
        j = S.get(url, timeout=30).json()
        cols = j["candles"]["columns"]
        data = j["candles"]["data"]
        if not data:
            break
        idx = {c: i for i, c in enumerate(cols)}
        for r in data:
            rows.append((secid, r[idx["begin"]][:10], r[idx["open"]], r[idx["high"]],
                         r[idx["low"]], r[idx["close"]], r[idx["volume"]]))
        start += len(data)
        if len(data) < 500 or start > 2000:
            break
    return rows


def fred(secid, fid):
    txt = S.get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s&cosd=%s" % (fid, FROM),
                timeout=45).text
    rows = []
    for line in txt.splitlines()[1:]:
        p = line.split(",")
        if len(p) >= 2 and p[1]:
            rows.append((secid, p[0], None, None, None, float(p[1]), None))
    return rows


def cbr_fx(secid, valcode):
    url = ("https://cbr.ru/scripts/XML_dynamic.asp?date_req1=01/11/2025"
           "&date_req2=18/10/2026&VAL_NM_RQ=%s" % valcode)
    resp = S.get(url, timeout=45)
    root = ET.fromstring(resp.content)
    rows = []
    for rec in root.iter("Record"):
        v = float(rec.findtext("Value").replace(",", "."))
        nom = float((rec.findtext("Nominal") or "1").replace(",", "."))
        d = rec.get("Date").split(".")
        rows.append((secid, "%s-%s-%s" % (d[2], d[1], d[0]), None, None, None, v / nom, None))
    return rows


def main():
    conn = psycopg2.connect(DSN)
    cur = conn.cursor()
    sources = [
        ("IMOEX", lambda: moex_history("stock", "index", "SNDX", "IMOEX")),
        ("CNYRUB_TOM", lambda: moex_candles("CNYRUB_TOM")),
        ("GLDRUB_TOM", lambda: moex_candles("GLDRUB_TOM")),
        ("USDRUB_TOM", lambda: moex_candles("USDRUB_TOM")),
        ("EURRUB_TOM", lambda: moex_candles("EURRUB_TOM")),
        ("BRENT", lambda: fred("BRENT", "DCOILBRENTEU")),
        ("NG", lambda: fred("NG", "DHHNGSP")),
    ]
    for name, fn in sources:
        try:
            rows = fn()
        except Exception as e:
            print("ERR %s: %s" % (name, e))
            continue
        if name in ("USDRUB_TOM", "EURRUB_TOM") and not rows:
            code = "R01235" if name.startswith("USD") else "R01239"
            try:
                rows = cbr_fx(name.replace("_TOM", ""), code)
            except Exception as e:
                print("ERR %s cbr fallback: %s" % (name, e))
                continue
        if rows:
            cur.executemany(UPSQL, rows)
            conn.commit()
            dts = sorted(r[1] for r in rows)
            print("OK %s: %d rows %s..%s" % (name, len(rows), dts[0], dts[-1]))
        else:
            print("EMPTY %s" % name)
    cur.execute("SELECT count(*) FROM macro_daily")
    print("TOTAL", cur.fetchone()[0])
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
