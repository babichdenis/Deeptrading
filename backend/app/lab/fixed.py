"""M3: fixed SL/TP (4 профиля ATR) на торговом пути 1m.

Контракт (architect, DECISIONS.md 2026-10-02):
- уровни от ATR сигнального бара: SL = entry ∓ sl_atr×ATR, TP = entry ± tp_atr×ATR;
- путь — сессионные 1m-бары (morning/day/evening, пн–пт) от anchor включительно;
- гэп через уровень → выход по open; конфликт SL+TP в одном 1m-баре → STOP_LOSS_FIRST;
- таймаут = timeout_bars баров source_tf; выход в конце торгового дня (23:50 МСК)
  → SESSION_CLOSE (overnight=False, «как торговал бы бот»);
- издержки — CostModel (canonical): fill_price обеих ног + комиссия;
- запись — DELETE по figi + COPY (идемпотентно, быстро).
"""
from __future__ import annotations

import hashlib
import io
import json

import numpy as np
from sqlalchemy import text

from app.engine.costs import CostModel
from app.engine.models import Side
from app.lab.data import engine_sync, iso, load_1m
from app.lab.outcomes import _session_mask_np, _to_ns
from app.models.signal_lab import LabFixedExit

_COLS = ("signal_id", "profile_code", "sl_atr", "tp_atr", "atr_entry", "atr_period",
         "entry_ts", "entry_px", "exit_ts", "exit_px", "exit_reason", "bars_held_1m",
         "bars_held_tf", "gross_return", "net_return", "r_multiple",
         "sl_first_conflict", "gap_open", "cfg_hash")


def fixed_cfg_hash(profiles: list[dict], timeout_bars: int, atr_period: int,
                   sessions: list[str], costs: dict) -> str:
    raw = json.dumps({"p": profiles, "t": timeout_bars, "atr": atr_period,
                      "s": sessions, "c": costs, "impl": "fixed_v1"},
                     sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _first_true(mask: np.ndarray) -> int | None:
    w = np.flatnonzero(mask)
    return int(w[0]) if len(w) else None


def fixed_task(args: dict) -> dict:
    """Worker: один тикер → fixed-симуляции всех его сигналов → COPY в БД."""
    figi = args["figi"]
    res: dict = {"figi": figi, "ticker": args.get("ticker", figi), "rows": 0,
                 "no_atr": 0, "reasons": {}, "error": None}
    try:
        eng = engine_sync(args["db_url"])
        with eng.connect() as c:
            sig_rows = c.execute(text(
                "SELECT id, side, tf_seconds, anchor_ts, entry_px, atr_entry "
                "FROM lab_signal_events WHERE run_id = :r AND figi = :f "
                "AND path_status = 'OK' ORDER BY bar_ts"
            ), {"r": args["run_id"], "f": figi}).fetchall()
        if not sig_rows:
            return res
        # окно 1m: от минимального anchor до + timeout (плюс сутки запаса)
        tmax_min = max(int(r[2]) for r in sig_rows) * int(args["timeout_bars"]) // 60
        from datetime import datetime, timedelta, timezone as _tz

        def _p(v):
            if isinstance(v, datetime):
                return v if v.tzinfo else v.replace(tzinfo=_tz.utc)
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=_tz.utc)

        lo = min(_p(r[3]) for r in sig_rows) - timedelta(days=1)
        hi = max(_p(r[3]) for r in sig_rows) + timedelta(minutes=tmax_min + 1440)
        bars = load_1m(eng, figi, iso(lo), iso(hi))
        if not bars:
            res["error"] = "no_1m_bars"
            return res
        ts_ns = _to_ns(bars)
        o = np.asarray([b.open for b in bars], dtype=np.float64)
        h = np.asarray([b.high for b in bars], dtype=np.float64)
        l = np.asarray([b.low for b in bars], dtype=np.float64)
        cl = np.asarray([b.close for b in bars], dtype=np.float64)
        mask = _session_mask_np(ts_ns)
        sess = np.flatnonzero(mask)
        day = (ts_ns // 1_000_000_000 + 3 * 3600) // 86400  # МСК-дата числом
        cm = CostModel(commission_rate=float(args["costs"]["commission_rate"]),
                       slippage_bps=float(args["costs"]["slippage_bps"]))
        profiles = args["profiles"]
        cfg_h = args["cfg_hash"]
        atr_period = int(args["atr_period"])

        out_rows: list[tuple] = []
        for r in sig_rows:
            sid, side, tsec, anchor_ts, entry_px, atr_entry = (
                int(r[0]), str(r[1]), int(r[2]), _p(r[3]), float(r[4]),
                (float(r[5]) if r[5] else None))
            if atr_entry is None or (entry_px and atr_entry / entry_px < 1e-5):
                res["no_atr"] += 1
                for p in profiles:
                    out_rows.append((sid, p["code"], p["sl_atr"], p["tp_atr"], None, atr_period,
                                     iso(anchor_ts), entry_px, None, None, "NO_ATR", 0, 0,
                                     None, None, None, False, False, cfg_h))
                continue
            a_ns = int(anchor_ts.timestamp() * 1_000_000_000)
            j = int(np.searchsorted(ts_ns, a_ns))
            if j >= len(bars) or int(ts_ns[j]) != a_ns:
                continue
            a = int(np.searchsorted(sess, j))
            n = int(args["timeout_bars"]) * tsec // 60
            idx = sess[a:a + n]
            if len(idx) == 0:
                continue
            dates = day[idx]
            ch = np.flatnonzero(dates != dates[0])
            day_len = int(ch[0]) if len(ch) else len(idx)
            is_long = side.upper() in ("BUY", "LONG")
            entry_side = Side.BUY if is_long else Side.SELL
            exit_side = Side.SELL if is_long else Side.BUY
            entry_fill = cm.fill_price(entry_px, entry_side)
            op = o[idx]
            hi = h[idx]
            lo = l[idx]
            cc = cl[idx]
            limit = min(n, day_len, len(idx))
            for p in profiles:
                sl_atr = float(p["sl_atr"])
                tp_atr = float(p["tp_atr"])
                risk = sl_atr * atr_entry
                sl = entry_px - risk if is_long else entry_px + risk
                tp = entry_px + tp_atr * atr_entry if is_long else entry_px - tp_atr * atr_entry
                if is_long:
                    sl_mask = (lo <= sl) | (op <= sl)
                    tp_mask = (hi >= tp) | (op >= tp)
                else:
                    sl_mask = (hi >= sl) | (op >= sl)
                    tp_mask = (lo <= tp) | (op <= tp)
                sl_i = _first_true(sl_mask[:limit])
                tp_i = _first_true(tp_mask[:limit])
                conflict = sl_i is not None and tp_i is not None and sl_i == tp_i
                gap = False
                if sl_i is not None and (tp_i is None or sl_i <= tp_i):
                    i_x = sl_i
                    exit_reason = "SL"
                    beyond = (op[i_x] <= sl) if is_long else (op[i_x] >= sl)
                    gap = bool(beyond)
                    raw = float(op[i_x]) if beyond else sl
                elif tp_i is not None:
                    i_x = tp_i
                    exit_reason = "TP"
                    beyond = (op[i_x] >= tp) if is_long else (op[i_x] <= tp)
                    gap = bool(beyond)
                    raw = float(op[i_x]) if beyond else tp
                elif limit < min(n, len(idx)):
                    i_x = limit - 1
                    exit_reason = "SESSION_CLOSE"
                    raw = float(cc[i_x])
                else:
                    i_x = min(n, len(idx)) - 1
                    exit_reason = "TIMEOUT"
                    raw = float(cc[i_x])
                exit_fill = cm.fill_price(raw, exit_side)
                gross = (exit_fill - entry_fill) / entry_fill * (1.0 if is_long else -1.0)
                comm = cm.commission_rate
                net = gross - comm - comm * (exit_fill / entry_fill)
                r_mult = net / (risk / entry_px) if risk > 0 else None
                bars1 = i_x + 1
                out_rows.append((
                    sid, p["code"], sl_atr, tp_atr, atr_entry, atr_period,
                    iso(anchor_ts), entry_px, iso(bars[idx[i_x]].ts), raw, exit_reason,
                    bars1, int(np.ceil(bars1 * 60 / tsec)), gross, net, r_mult,
                    bool(conflict), gap, cfg_h,
                ))
                res["reasons"][exit_reason] = res["reasons"].get(exit_reason, 0) + 1
        # запись: DELETE по figi + COPY (быстро и идемпотентно)
        with eng.begin() as c:
            c.execute(text(
                "DELETE FROM lab_fixed_exits f USING lab_signal_events s "
                "WHERE f.signal_id = s.id AND s.run_id = :r AND s.figi = :f"
            ), {"r": args["run_id"], "f": figi})
            drv = c.connection.driver_connection
            cur = drv.cursor()
            try:
                buf = io.StringIO()
                for row in out_rows:
                    buf.write("\t".join("\\N" if v is None else str(v) for v in row) + "\n")
                buf.seek(0)
                cur.copy_expert(
                    f"COPY lab_fixed_exits ({', '.join(_COLS)}) FROM STDIN", buf)
            finally:
                cur.close()
        res["rows"] = len(out_rows)
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return res
