"""Событизация сигналов: свернуть серии одинаковых сигналов в события.

Ключ: figi × tf × engine. Новый event, если:
  - сигнал появился (старт серии);
  - сменилась сторона BUY → SELL;
  - разрыв временной последовательности (пропущен бар: ts != prev + tf).
Сырой слой lab_signal_events не трогаем — сравнение raw → event остаётся.
Canonical entry point события = start_ts (outcomes берутся по start_signal_id).
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone

from sqlalchemy import text

from app.lab.data import engine_sync, iso

_COLS = ("run_id", "figi", "ticker", "strategy_id", "tf", "tf_seconds", "side",
         "start_ts", "start_close_ts", "end_ts", "duration_bars", "signal_count",
         "start_signal_id", "created_at")


def _p(v) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def events_task(args: dict) -> dict:
    """Worker: один тикер → события по всем движкам и ТФ → COPY в БД."""
    figi = args["figi"]
    res: dict = {"figi": figi, "ticker": args.get("ticker", figi), "events": 0,
                 "by_engine": {}, "error": None}
    try:
        eng = engine_sync(args["db_url"])
        rows = []
        with eng.connect() as c:
            cur = c.execute(text(
                "SELECT id, strategy_id, tf, tf_seconds, bar_ts, side "
                "FROM lab_signal_events WHERE run_id = :r AND figi = :f "
                "ORDER BY strategy_id, tf_seconds, bar_ts"
            ), {"r": args["run_id"], "f": figi})
            rows = cur.fetchall()
        if not rows:
            return res
        now = datetime.now(timezone.utc).isoformat()
        out: list[tuple] = []
        cur_engine = None
        cur_tf = None
        prev_side = None
        prev_ts = None
        ev = None

        def flush():
            if ev is None:
                return
            dur = int((ev["end"] - ev["start"]).total_seconds() // ev["tf_sec"]) + 1
            out.append((args["run_id"], figi, res["ticker"], ev["engine"], ev["tf"],
                        ev["tf_sec"], ev["side"], iso(ev["start"]),
                        iso(ev["start"] + __import__("datetime").timedelta(seconds=ev["tf_sec"])),
                        iso(ev["end"]), dur, ev["count"], ev["sid"], now))
            res["by_engine"][ev["engine"]] = res["by_engine"].get(ev["engine"], 0) + 1

        for r in rows:
            sid, engine, tf, tf_sec, bar_ts, side = (
                int(r[0]), str(r[1]), str(r[2]), int(r[3]), _p(r[4]), str(r[5]))
            new_engine = (engine != cur_engine) or (tf != cur_tf)
            gap = prev_ts is not None and (bar_ts - prev_ts).total_seconds() != tf_sec
            if new_engine or ev is None:
                flush()
                ev = {"engine": engine, "tf": tf, "tf_sec": tf_sec, "side": side,
                      "start": bar_ts, "end": bar_ts, "count": 1, "sid": sid}
            elif side != prev_side or gap:
                flush()
                ev = {"engine": engine, "tf": tf, "tf_sec": tf_sec, "side": side,
                      "start": bar_ts, "end": bar_ts, "count": 1, "sid": sid}
            else:
                ev["end"] = bar_ts
                ev["count"] += 1
            cur_engine, cur_tf = engine, tf
            prev_side, prev_ts = side, bar_ts
        flush()

        with eng.begin() as c:
            c.execute(text(
                "DELETE FROM lab_signal_event_runs WHERE run_id = :r AND figi = :f"
            ), {"r": args["run_id"], "f": figi})
            drv = c.connection.driver_connection
            dbc = drv.cursor()
            try:
                buf = io.StringIO()
                for row in out:
                    buf.write("\t".join("\\N" if v is None else str(v) for v in row) + "\n")
                buf.seek(0)
                dbc.copy_expert(
                    f"COPY lab_signal_event_runs ({', '.join(_COLS)}) FROM STDIN", buf)
            finally:
                dbc.close()
        res["events"] = len(out)
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return res
