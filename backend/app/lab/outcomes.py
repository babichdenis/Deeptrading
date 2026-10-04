"""M2: path-independent исходы сигналов (горизонты, MFE/MAE).

Торговый путь — 1m-бары в сессиях morning/day/evening, пн–пт (без ночи и
выходных; решение владельца 2026-10-02). anchor = первый сессионный 1m-бар
>= bar_close_ts; entry = open(anchor). Горизонт h (в барах source_tf) =
h × tf_seconds МИНУТ ТОРГОВОГО ВРЕМЕНИ вперёд (полуоткрытое окно). MFE/MAE —
по стороне сигнала: BUY: high/entry−1 и low/entry−1; SELL зеркально.
Барьеры (TP/SL) здесь НЕ считаются — это слои fixed/trailing.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import numpy as np
from psycopg2.extras import execute_values
from sqlalchemy import text

from app.engine.sessions import SESSION_WINDOWS, is_session_active
from app.lab.data import atr_series, build_tf, engine_sync, iso, load_1m
from app.models.signal_lab import LabMarketOutcome


def outcome_cfg_hash(horizons: list[int], atr_period: int, sessions: list[str]) -> str:
    raw = json.dumps({"h": horizons, "atr": atr_period, "s": sessions},
                     sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def outcome_cfg_hash_impl(horizons: list[int], atr_period: int, sessions: list[str],
                          impl: str = "numpy") -> str:
    """Хэш конфига исхода с меткой реализации (parity-сравнение old/vec)."""
    raw = json.dumps({"h": horizons, "atr": atr_period, "s": sessions, "impl": impl},
                     sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _session_mask_np(ts_ns: np.ndarray) -> np.ndarray:
    """Векторный аналог is_session_active: MSK, окна сессий, пн–пт.

    Проверяется на паритет с is_session_active в outcomes_task (sample).
    """
    sec = ts_ns // 1_000_000_000 + 3 * 3600          # UTC → МСК
    wd = (sec // 86400 + 3) % 7                       # epoch был четверг → пн=0
    mins = (sec % 86400) // 60
    m = np.zeros(sec.shape, dtype=bool)
    for name in ("morning", "day", "evening"):
        a, b = SESSION_WINDOWS[name]
        m |= (mins >= a) & (mins < b)
    return m & (wd < 5)


def _to_ns(bars) -> np.ndarray:
    return np.asarray([b.ts for b in bars], dtype="datetime64[ns]").astype(np.int64)


def _parse(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def outcomes_task(args: dict) -> dict:
    """Worker: один тикер → все его сигналы run'а → outcome-строки + апдейты."""
    import time as _t
    _t0 = _t.perf_counter()
    figi = args["figi"]
    res: dict = {"figi": figi, "ticker": args.get("ticker", figi), "outcomes": [],
                 "updates": [], "skipped": 0, "error": None,
                 "sec_load": 0.0, "sec_compute": 0.0}
    try:
        eng = engine_sync(args["db_url"])
        with eng.connect() as c:
            sig_rows = c.execute(text(
                "SELECT id, side, tf, tf_seconds, bar_ts, bar_close_ts FROM lab_signal_events "
                "WHERE run_id = :r AND figi = :f ORDER BY bar_ts"
            ), {"r": args["run_id"], "f": figi}).fetchall()
        if not sig_rows:
            return res
        horizons = [int(h) for h in args["horizons"]]
        h_max_min = max(horizons) * max(int(r[3]) for r in sig_rows) // 60
        lo = min(_parse(r[5]) for r in sig_rows) - timedelta(days=1)
        hi = max(_parse(r[5]) for r in sig_rows) + timedelta(minutes=h_max_min + 1440)
        bars_1m = load_1m(eng, figi, iso(lo), iso(hi))
        if not bars_1m:
            res["error"] = "no_1m_bars"
            return res
        # numpy-представление пути (все тяжёлые операции — по массивам)
        ts_ns = _to_ns(bars_1m)
        o_arr = np.asarray([b.open for b in bars_1m], dtype=np.float64)
        h_arr = np.asarray([b.high for b in bars_1m], dtype=np.float64)
        l_arr = np.asarray([b.low for b in bars_1m], dtype=np.float64)
        c_arr = np.asarray([b.close for b in bars_1m], dtype=np.float64)
        mask = _session_mask_np(ts_ns)
        # паритет маски с is_session_active (по выборке — страховка от расхождения)
        _chk = np.arange(0, len(bars_1m), max(1, len(bars_1m) // 500))
        _sess = ["morning", "day", "evening"]
        for _i in _chk:
            if bool(mask[_i]) != bool(is_session_active(bars_1m[_i].ts, _sess)):
                res["error"] = f"session_mask mismatch at {bars_1m[_i].ts}"
                return res
        sess = np.flatnonzero(mask)
        res["sec_load"] = round(_t.perf_counter() - _t0, 2)
        _tc = _t.perf_counter()
        # ATR по каждому ТФ сигналов (считаем один раз)
        atr_map: dict[str, list] = {}
        tf_bars: dict[str, list] = {}
        tf_ts_ns: dict[str, np.ndarray] = {}
        for tf in sorted({str(r[2]) for r in sig_rows}):
            tf_bars[tf] = build_tf(bars_1m, tf)
            tf_ts_ns[tf] = _to_ns(tf_bars[tf])
            atr_map[tf] = atr_series(tf_bars[tf], int(args["atr_period"]))
        cfg_h = args["cfg_hash"]

        for r in sig_rows:
            sid, side, tf, tsec, bar_ts, bar_close = (r[0], str(r[1]), str(r[2]),
                                                      int(r[3]), _parse(r[4]), _parse(r[5]))
            bar_ns = int(bar_ts.timestamp() * 1_000_000_000)
            close_ns = int(bar_close.timestamp() * 1_000_000_000)
            entry_px = None
            anchor_ts = None
            gap_min = None
            status = "OK"
            a = int(np.searchsorted(sess, int(np.searchsorted(ts_ns, close_ns)), side="left"))
            if a >= len(sess):
                status = "NO_ANCHOR"
            else:
                j = int(sess[a])
                anchor = bars_1m[j]
                anchor_ts = iso(anchor.ts)
                entry_px = float(o_arr[j])
                gap_min = int((anchor.ts - bar_close).total_seconds() // 60)
            atr_entry = None
            if tf in tf_bars:
                k2 = int(np.searchsorted(tf_ts_ns[tf], bar_ns))
                if k2 < len(tf_ts_ns[tf]) and int(tf_ts_ns[tf][k2]) == bar_ns:
                    av = atr_map[tf][k2]
                    atr_entry = float(av) if av else None
            res["updates"].append((sid, anchor_ts, entry_px, gap_min, status, atr_entry))

            # Вырожденный ATR (синтетические бары ~1e-13) — метрики по ATR мусорные
            if (status == "OK" and atr_entry is not None and entry_px
                    and atr_entry / entry_px < 1e-5):
                status = "BAD_ATR"
                atr_entry = None
                res["updates"][-1] = (sid, anchor_ts, entry_px, gap_min, status, None)
            if status != "OK" or entry_px is None:
                res["skipped"] += 1
                continue
            n_max = max(int(h * tsec // 60) for h in horizons)
            path = sess[a:a + n_max]
            if len(path) == 0:
                res["skipped"] += 1
                continue
            is_long = side.upper() in ("BUY", "LONG")
            for h in horizons:
                n = int(h * tsec // 60)
                m = min(n, len(path))
                if m <= 0:
                    continue
                idx = path[:m]
                end_bar = bars_1m[int(idx[-1])]
                end_close = float(c_arr[idx[-1]])
                if is_long:
                    fav = h_arr[idx] / entry_px - 1.0
                    adv = l_arr[idx] / entry_px - 1.0
                else:
                    fav = (entry_px - l_arr[idx]) / entry_px
                    adv = (entry_px - h_arr[idx]) / entry_px
                t_mfe = int(fav.argmax())
                t_mae = int(adv.argmin())
                mfe = float(fav[t_mfe])
                mae = float(adv[t_mae])
                res["outcomes"].append({
                    "signal_id": sid,
                    "horizon_bars": h,
                    "horizon_minutes": int(h * tsec // 60),
                    "anchor_ts": anchor_ts,
                    "entry_px": entry_px,
                    "end_ts": iso(end_bar.ts),
                    "bars_1m": m,
                    "future_return": (end_close - entry_px) / entry_px,
                    "future_return_atr": ((end_close - entry_px) / atr_entry)
                        if atr_entry else None,
                    "mfe": mfe,
                    "mae": mae,
                    "mfe_atr": (mfe * entry_px / atr_entry) if (mfe is not None and atr_entry) else None,
                    "mae_atr": (mae * entry_px / atr_entry) if (mae is not None and atr_entry) else None,
                    "mfe_ts": iso(bars_1m[int(idx[t_mfe])].ts),
                    "mae_ts": iso(bars_1m[int(idx[t_mae])].ts),
                    "t_mfe_bars_1m": t_mfe,
                    "t_mae_bars_1m": t_mae,
                    "atr_entry": atr_entry,
                    "atr_period": int(args["atr_period"]),
                    "outcome_cfg_hash": cfg_h,
                })
        res["sec_compute"] = round(_t.perf_counter() - _tc, 2)
        if args.get("persist"):
            # Воркер пишет сам: параллельные коннекты + никакой передачи миллионов
            # строк через IPC родителю. Idempotent (ON CONFLICT), figis не пересекаются.
            u, i = persist_outcomes(eng, res)
            res["n_upd"], res["n_ins"] = u, i
            res["updates"], res["outcomes"] = [], []
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return res


def persist_outcomes(eng, res: dict) -> tuple[int, int]:
    """UPDATE-ы сигналов + INSERT исходов (идемпотентно, батчами одним запросом)."""
    import time as _t
    _t0 = _t.perf_counter()
    upd = res.get("updates") or []
    ins = res.get("outcomes") or []
    n_ins = 0
    with eng.begin() as c:
        # Один UPDATE на чанк через unnest массивов — вместо N построчных round-trip
        # (было 55с на 4.6k сигналов, стало ~1с).
        for i in range(0, len(upd), 5000):
            ch = upd[i:i + 5000]
            c.execute(text(
                "UPDATE lab_signal_events AS s SET "
                "anchor_ts = u.a, entry_px = u.e, anchor_gap_min = u.g, "
                "path_status = u.st, atr_entry = u.t "
                "FROM (SELECT unnest(CAST(:ids AS bigint[])) AS id, "
                "unnest(CAST(:a AS timestamptz[])) AS a, unnest(CAST(:e AS float8[])) AS e, "
                "unnest(CAST(:g AS int[])) AS g, unnest(CAST(:st AS text[])) AS st, "
                "unnest(CAST(:t AS float8[])) AS t) AS u WHERE s.id = u.id"
            ), {
                "ids": [x[0] for x in ch], "a": [x[1] for x in ch],
                "e": [x[2] for x in ch], "g": [x[3] for x in ch],
                "st": [x[4] for x in ch], "t": [x[5] for x in ch],
            })
        res["persist_upd_sec"] = round(_t.perf_counter() - _t0, 2)
        _t1 = _t.perf_counter()
        if ins:
            # execute_values: один INSERT на страницу без компиляции SQLAlchemy
            # (многотысячные VALUES у неё компилируются секундами).
            cols = ("signal_id", "horizon_bars", "horizon_minutes", "anchor_ts", "entry_px",
                    "end_ts", "bars_1m", "future_return", "future_return_atr", "mfe", "mae",
                    "mfe_atr", "mae_atr", "mfe_ts", "mae_ts", "t_mfe_bars_1m", "t_mae_bars_1m",
                    "atr_entry", "atr_period", "outcome_cfg_hash")
            sql = (f"INSERT INTO lab_market_outcomes ({', '.join(cols)}) VALUES %s "
                   "ON CONFLICT ON CONSTRAINT uq_lab_outcome DO NOTHING")
            drv = c.connection.driver_connection
            cur = drv.cursor()
            try:
                for i in range(0, len(ins), 5000):
                    part = [tuple(d[k] for k in cols) for d in ins[i:i + 5000]]
                    execute_values(cur, sql, part, page_size=5000)
                    n_ins += cur.rowcount or 0
            finally:
                cur.close()
        res["persist_ins_sec"] = round(_t.perf_counter() - _t1, 2)
    return len(upd), n_ins