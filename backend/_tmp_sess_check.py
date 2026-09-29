import asyncio, json, os, time, urllib.request
from sqlalchemy import text
from app.database import SessionLocal
from app.bot.moex import IMOEX_FIGI


def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=12) as r:
        return json.load(r)


async def main():
    # 1) живой статус бота
    try:
        st = get("/api/v1/bot/status")
        c = st.get("config") or {}
        print("=== БОТ ===")
        print("running:", st.get("running"), "| mode:", st.get("mode"))
        for k in ("sessions", "margin_sessions", "overnight", "feed",
                  "replay_start", "replay_end", "test_name"):
            print("  cfg.%s = %s" % (k, c.get(k)))
    except Exception as e:
        print("status err:", e)

    try:
        bc = json.load(open("data/bot_config.json", encoding="utf-8"))
        print("bot_config.json: sessions=%s margin=%s overnight=%s mtime=%s" % (
            bc.get("sessions"), bc.get("margin_sessions"), bc.get("overnight"),
            time.strftime("%d.%m %H:%M", time.localtime(os.path.getmtime("data/bot_config.json")))))
    except Exception as e:
        print("bot_config err:", e)

    # 2) лог-кольцо: виртуальное время + IMOEX
    try:
        logs = get("/api/v1/bot/logs?limit=2000").get("logs") or []
        candles = [x for x in logs if "Свеча" in (x.get("msg") or "")]
        print("\nлогов в кольце: %d | последняя свеча: %s" % (
            len(logs), (candles[-1].get("msg") or "")[:90] if candles else "нет"))
        guard = [x for x in logs if "IMOEX" in (x.get("msg") or "")]
        blk = [x for x in logs if "imoex_guard" in (x.get("msg") or "")]
        sess = [x for x in logs if "вне торговых сессий" in (x.get("msg") or "")]
        print("IMOEX-сообщений: %d | блоков imoex_guard: %d | «вне сессий»: %d" % (
            len(guard), len(blk), len(sess)))
        for x in guard[-5:]:
            print("  ", (x.get("ts") or "")[11:19], (x.get("msg") or "")[:110])
        for x in sess[-3:]:
            print("  SESS", (x.get("ts") or "")[11:19], (x.get("msg") or "")[:110])
    except Exception as e:
        print("logs err:", e)

    # 3) сводка тестов
    try:
        ts = get("/api/v1/bot/tests")
        print("\n=== /bot/tests ===")
        for t in ts.get("tests", []):
            print("  %-14s trades=%4d wins=%4d net=%10s open=%d" % (
                t.get("name"), t.get("trades", 0), t.get("wins", 0), t.get("net"), t.get("positions_open", 0)))
    except Exception as e:
        print("tests err:", e)

    async with SessionLocal() as db:
        # 4) сделки по сессиям (вход МСК)
        print("\n=== СДЕЛКИ ПО СЕССИЯМ (вход МСК; закрытые: n / net; откр. отдельно) ===")
        sql = """
        SELECT test_name,
          count(*) FILTER (WHERE s='m' AND ex), round(coalesce(sum(p) FILTER (WHERE s='m' AND ex),0)::numeric,2),
          count(*) FILTER (WHERE s='d' AND ex), round(coalesce(sum(p) FILTER (WHERE s='d' AND ex),0)::numeric,2),
          count(*) FILTER (WHERE s='e' AND ex), round(coalesce(sum(p) FILTER (WHERE s='e' AND ex),0)::numeric,2),
          count(*) FILTER (WHERE NOT ex)
        FROM (
          SELECT test_name, exit_time IS NOT NULL AS ex, net_pnl AS p,
            CASE WHEN (entry_time AT TIME ZONE 'Europe/Moscow')::time < time '09:50' THEN 'm'
                 WHEN (entry_time AT TIME ZONE 'Europe/Moscow')::time < time '19:00' THEN 'd'
                 ELSE 'e' END AS s
          FROM sandbox_trades WHERE mode='paper') t
        GROUP BY 1 ORDER BY 1"""
        r = await db.execute(text(sql))
        print("  тест            | утро n/net       | день n/net        | вечер n/net      | откр")
        for a, bm, nm, bd, nd2, be, ne, no in r.all():
            print("  %-14s | %3d / %+9.2f | %3d / %+9.2f | %3d / %+8.2f | %d" % (
                str(a), bm, float(nm), bd, float(nd2), be, float(ne), no))

        # 5) MACD1m_v2: качество по сессиям
        print("\n=== MACD1m_v2: качество по сессиям (закрытые) ===")
        sql2 = """
        SELECT s, count(*), count(*) FILTER (WHERE p>0), round(sum(p)::numeric,2), round(avg(p)::numeric,2)
        FROM (SELECT net_pnl AS p,
            CASE WHEN (entry_time AT TIME ZONE 'Europe/Moscow')::time < time '09:50' THEN 'утро<9:50'
                 WHEN (entry_time AT TIME ZONE 'Europe/Moscow')::time < time '19:00' THEN 'день'
                 ELSE 'вечер>=19' END AS s
            FROM sandbox_trades WHERE mode='paper' AND test_name='MACD1m_v2' AND exit_time IS NOT NULL) t
        GROUP BY 1 ORDER BY 1"""
        r = await db.execute(text(sql2))
        for a, b, w, net, avg in r.all():
            print("  %-10s n=%3d побед=%3d wr=%4.1f%% net=%+9.2f avg=%+7.2f" % (
                str(a), b, w, 100.0 * w / max(b, 1), float(net), float(avg)))

        # 6) MACDvol: ход прогона
        print("\n=== MACDvol: все сделки (МСК) ===")
        r = await db.execute(text(
            "SELECT to_char(entry_time AT TIME ZONE 'Europe/Moscow','DD.MM HH24:MI'), ticker, side, net_pnl, "
            "coalesce(exit_reason,'OPEN'), to_char(exit_time AT TIME ZONE 'Europe/Moscow','HH24:MI') "
            "FROM sandbox_trades WHERE mode='paper' AND test_name='MACDvol' ORDER BY entry_time"))
        for a, b, s2, p, xr, xt in r.all():
            print("  %s %-6s %-5s net=%-10s %-24s %s" % (
                a, str(b), str(s2), str(p), str(xr), str(xt or "")))

        # 7) overnight-закрытия
        print("\n=== overnight_force_close по тестам ===")
        r = await db.execute(text(
            "SELECT test_name, count(*), round(sum(net_pnl)::numeric,2), "
            "to_char(min(exit_time AT TIME ZONE 'Europe/Moscow'),'HH24:MI'), "
            "to_char(max(exit_time AT TIME ZONE 'Europe/Moscow'),'HH24:MI') "
            "FROM sandbox_trades WHERE mode='paper' AND exit_reason='overnight_force_close' "
            "GROUP BY 1 ORDER BY 1"))
        for a, b, net, mn, mx in r.all():
            print("  %-14s n=%3d net=%+9.2f выходы %s..%s МСК" % (str(a), b, float(net), mn, mx))

        # 8) IMOEX: покрытие в БД
        print("\n=== IMOEX в БД (figi=%s) ===" % IMOEX_FIGI)
        r = await db.execute(text(
            "SELECT interval, count(*), to_char(min(ts),'DD.MM HH24:MI'), to_char(max(ts),'DD.MM HH24:MI') "
            "FROM candles WHERE figi=:f GROUP BY 1 ORDER BY 1"), {"f": IMOEX_FIGI})
        for a, b, mn, mx in r.all():
            print("  interval=%s n=%d %s → %s (UTC)" % (a, b, mn, mx))
        r = await db.execute(text(
            "SELECT ts::date, count(*), to_char(min(ts) AT TIME ZONE 'Europe/Moscow','HH24:MI'), "
            "to_char(max(ts) AT TIME ZONE 'Europe/Moscow','HH24:MI') "
            "FROM candles WHERE figi=:f AND interval=1 AND ts >= '2026-09-12' GROUP BY 1 ORDER BY 1"),
            {"f": IMOEX_FIGI})
        print("  1m по дням (границы МСК):")
        for a, b, mn, mx in r.all():
            print("    %s n=%d %s→%s" % (a, b, mn, mx))
        r = await db.execute(text(
            "SELECT extract(hour from ts AT TIME ZONE 'Europe/Moscow') h, count(*) "
            "FROM candles WHERE figi=:f AND interval=1 AND ts::date='2026-09-15' GROUP BY 1 ORDER BY 1"),
            {"f": IMOEX_FIGI})
        print("  1m по часам МСК 15.09:", [(int(h), int(n)) for h, n in r.all()])

        # 9) окна тестов
        r = await db.execute(text(
            "SELECT name, to_char(replay_start AT TIME ZONE 'Europe/Moscow','DD.MM HH24:MI'), "
            "to_char(replay_end AT TIME ZONE 'Europe/Moscow','DD.MM HH24:MI') "
            "FROM bot_test_runs ORDER BY updated_at DESC LIMIT 8"))
        print("\nbot_test_runs (окна, МСК):")
        for a, b, c2 in r.all():
            print("  %-14s %s → %s" % (str(a), b, c2))


asyncio.run(main())
