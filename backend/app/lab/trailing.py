"""M4: trailing-симуляции (T1–T3) на торговом пути 1m.

Модели (дефолты из DECISIONS.md 2026-10-02):
  T1 bot_atr_comm4   — AtrStopPolicy: mult 4.0, rr 4.0, act=comm×4, dist 2.5×ATR,
                       compress 1.0, min_factor 0.6, min_atr 1.5, vol_boost 0.3 (бот);
  T2 atr_stop_r_mode — AtrStopPolicy: mult 2.0, rr 2.0, act=1.0R, dist 2.0R;
  T3 atr_trailing_legacy — initial_stop 2×ATR, activation 1×ATR, trail 2×ATR, без TP.
T4 exit_manager_default — отложен (интеграция ExitManager — следующий шаг).

Формулы 1:1 из app/engine/exits.py, но на предвычисленных ATR/vol рядах ТФ:
значение ATR(period) на баре i зависит только от баров ≤ i, поэтому
предвычисленный ряд == atr(растущего окна)[-1] (паритет-тест отдельно).
Обновление стопа — на закрытии ТФ-бара; проверка выхода — по 1m; новый стоп
действует со следующего 1m-бара; после активации — close-based, TP выключен.
"""
from __future__ import annotations

import hashlib
import io
import json
from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import text

from app.engine.costs import CostModel
from app.engine.models import Side
from app.lab.data import engine_sync, iso, load_1m
from app.lab.outcomes import _session_mask_np, _to_ns

_COLS = ("signal_id", "model_code", "policy_id", "policy_version", "model_cfg_hash",
         "params", "entry_ts", "entry_px", "initial_stop", "initial_tp", "activated",
         "activate_ts", "trail_stop_final", "exit_ts", "exit_px", "exit_reason",
         "bars_held_1m", "bars_held_tf", "gross_return", "net_return", "r_multiple")


def trailing_cfg_hash(profiles: list[dict], timeout_bars: int, atr_period: int,
                      sessions: list[str], costs: dict) -> str:
    raw = json.dumps({"p": profiles, "t": timeout_bars, "atr": atr_period,
                      "s": sessions, "c": costs, "impl": "trailing_v1"},
                     sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _atr_series_of(bars) -> list:
    from app.lab.data import atr_series
    return atr_series(bars, 14)


def _vol50(bars) -> np.ndarray:
    """Средний объём последних 50 ТФ-баров (включая текущий) — как в vol_boost."""
    v = np.asarray([float(b.volume or 0.0) for b in bars], dtype=np.float64)
    out = np.zeros(len(v), dtype=np.float64)
    c = np.cumsum(v)
    for i in range(len(v)):
        lo = max(0, i - 49)
        out[i] = (c[i] - (c[lo - 1] if lo > 0 else 0.0)) / (i - lo + 1)
    return out


def _bucket_ids(ts_ns: np.ndarray, tf_sec: int) -> np.ndarray:
    return (ts_ns // 1_000_000_000) // tf_sec


def _t1_stop(side_long: bool, peak: float, atr_t: float, dist_atr: float,
             close: float, entry: float, atr_mult: float, compress_r: float,
             min_factor: float, min_atr: float, vol_boost: float, vol_ratio: float,
             cur_stop: float) -> float:
    """AtrStopPolicy.update_stop (commission/R-ветка) без класса — те же формулы."""
    dist_unit = atr_t if atr_t else entry * 0.01
    dist = dist_atr * dist_unit
    if compress_r > 0:
        risk = atr_t * atr_mult
        if risk > 0:
            r = ((close - entry) if side_long else (entry - close)) / risk
            factor = max(min_factor, 1.0 - compress_r * max(0.0, r))
            dist *= factor
    if vol_boost > 0:
        adj = min(1.0 + vol_boost, max(1.0 - vol_boost, vol_ratio ** 0.5))
        dist *= adj
    if min_atr > 0:
        dist = max(dist, min_atr * dist_unit)
    if side_long:
        cand = peak - dist
        return max(cur_stop or cand, cand)
    cand = peak + dist
    stop = cur_stop if cur_stop is not None else cand
    return min(stop, cand)


def trailing_task(args: dict) -> dict:
    figi = args["figi"]
    res: dict = {"figi": figi, "ticker": args.get("ticker", figi), "rows": 0,
                 "by_model": {}, "skipped_models": [], "error": None}
    try:
        eng = engine_sync(args["db_url"])
        with eng.connect() as c:
            sig_rows = c.execute(text(
                "SELECT id, side, tf, tf_seconds, anchor_ts, entry_px, atr_entry "
                "FROM lab_signal_events WHERE run_id = :r AND figi = :f "
                "AND path_status = 'OK' ORDER BY bar_ts"
            ), {"r": args["run_id"], "f": figi}).fetchall()
        if not sig_rows:
            return res
        tfs = sorted({str(r[2]) for r in sig_rows})
        tmax = max(int(r[3]) for r in sig_rows) * int(args["timeout_bars"]) // 60

        def _p(v):
            if isinstance(v, datetime):
                return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

        lo = min(_p(r[4]) for r in sig_rows) - timedelta(days=2)
        hi = max(_p(r[4]) for r in sig_rows) + timedelta(minutes=tmax + 2880)
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
        day = (ts_ns // 1_000_000_000 + 3 * 3600) // 86400

        tf_ctx: dict[str, dict] = {}
        for tf in tfs:
            tsec = int(next(r[3] for r in sig_rows if str(r[2]) == tf))
            from app.marketdata.resampler import Resampler
            rs = Resampler(tf)
            tfbars = []
            for x in bars:
                out = rs.feed(x)
                if out is not None:
                    tfbars.append(out)
            tfbars.extend(rs.flush())
            atr = _atr_series_of(tfbars)
            atr_arr = np.asarray([(float(a) if a else np.nan) for a in atr], dtype=np.float64)
            close_arr = np.asarray([bb.close for bb in tfbars], dtype=np.float64)
            vol_arr = _vol50(tfbars)
            ts_arr = _to_ns(tfbars)
            tf_ctx[tf] = {"tsec": tsec, "atr": atr_arr, "close": close_arr,
                          "vol": vol_arr, "ts": ts_arr, "tfbars": tfbars}

        cm = CostModel(commission_rate=float(args["costs"]["commission_rate"]),
                       slippage_bps=float(args["costs"]["slippage_bps"]))
        cfg_h = args["cfg_hash"]
        models = args["models"]
        out_rows: list[tuple] = []

        for r in sig_rows:
            sid, side, tf, tsec, anchor_ts, entry_px, atr_entry = (
                int(r[0]), str(r[1]), str(r[2]), int(r[3]), _p(r[4]), float(r[5]),
                (float(r[6]) if r[6] else None))
            ctx = tf_ctx.get(tf)
            if ctx is None:
                continue
            bad_atr = atr_entry is None or (entry_px and atr_entry / entry_px < 1e-5)
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
            limit = min(n, day_len, len(idx))
            is_long = side.upper() in ("BUY", "LONG")
            entry_fill = cm.fill_price(entry_px, Side.BUY if is_long else Side.SELL)
            comm = entry_px * float(args["costs"]["commission_rate"])
            bidx = _bucket_ids(ts_ns[idx], tsec)
            bounds = np.flatnonzero(np.diff(bidx) != 0)
            bset = set(int(x) for x in bounds)
            if len(idx):
                bset.add(len(idx) - 1)
            for m in models:
                code = m["code"]
                pid = m.get("policy_id", "")
                p = dict(m.get("params") or {})
                if bad_atr:
                    out_rows.append((sid, code, pid, "", cfg_h, json.dumps(p, ensure_ascii=False),
                                     iso(anchor_ts), entry_px, None, None, False, None, None,
                                     None, None, "NO_ATR", 0, 0, None, None, None))
                    continue
                if pid == "atr_stop":
                    mult = float(p.get("multiplier", 4.0))
                    rr = float(p.get("risk_reward") or 0.0)
                    act_r = p.get("trail_activation_r")
                    act_comm = p.get("trail_activation_comm_mult")
                    dist_atr = float(p.get("trail_distance_r", 2.5))
                    compress = float(p.get("trail_compress_r", 0.0) or 0.0)
                    minf = float(p.get("trail_min_factor", 0.3) or 0.3)
                    mina = float(p.get("trail_min_atr", 0.0) or 0.0)
                    vboost = float(p.get("trail_vol_boost", 0.0) or 0.0)
                elif pid == "atr_trailing":
                    mult = float(p.get("initial_stop_atr", 2.0))
                    rr = 0.0
                    act_r = float(p.get("activation_atr", 1.0))
                    act_comm = None
                    dist_atr = float(p.get("trail_distance_atr", 2.0))
                    compress = minf = mina = vboost = 0.0
                else:
                    if code not in res["skipped_models"]:
                        res["skipped_models"].append(code)
                    continue
                risk0 = mult * atr_entry
                init_stop = entry_px - risk0 if is_long else entry_px + risk0
                init_tp = (entry_px + rr * risk0 if is_long else entry_px - rr * risk0) if rr else None
                stop = init_stop
                tp = init_tp
                active = False
                act_ts = None
                stop_final = stop
                peak = entry_px
                hit = None
                for pi in range(limit):
                    gi = int(idx[pi])
                    if is_long:
                        peak = max(peak, float(h[gi]))
                    else:
                        peak = min(peak, float(l[gi]))
                    if hit is None:
                        if not active:
                            sl_bad = (float(l[gi]) <= stop) if is_long else (float(h[gi]) >= stop)
                            tp_bad = (tp is not None) and ((float(h[gi]) >= tp) if is_long else (float(l[gi]) <= tp))
                            if sl_bad:
                                op_b = float(o[gi])
                                beyond = (op_b <= stop) if is_long else (op_b >= stop)
                                hit = (pi, (op_b if beyond else stop), "SL", bool(beyond))
                            elif tp_bad:
                                op_b = float(o[gi])
                                beyond = (op_b >= tp) if is_long else (op_b <= tp)
                                hit = (pi, (op_b if beyond else tp), "TP", bool(beyond))
                        else:
                            op_b, cl_b = float(o[gi]), float(cl[gi])
                            if is_long and (op_b <= stop or cl_b <= stop):
                                hit = (pi, (op_b if op_b <= stop else cl_b), "TRAILING_STOP", bool(op_b <= stop))
                            elif (not is_long) and (op_b >= stop or cl_b >= stop):
                                hit = (pi, (op_b if op_b >= stop else cl_b), "TRAILING_STOP", bool(op_b >= stop))
                        if hit is None:
                            if pi + 1 == day_len and pi + 1 < min(n, len(idx)):
                                hit = (pi, float(cl[gi]), "SESSION_CLOSE", False)
                            elif pi + 1 == min(n, len(idx)):
                                hit = (pi, float(cl[gi]), "TIMEOUT", False)
                    if pi in bset and pi < len(idx):
                        tf_i = int(np.searchsorted(ctx["ts"], int(ts_ns[gi]), side="right")) - 1
                        if 0 <= tf_i < len(ctx["atr"]):
                            atr_t = ctx["atr"][tf_i]
                            close_t = ctx["close"][tf_i]
                            ratio = 1.0
                            if not np.isfinite(atr_t):
                                atr_t = 0.0
                            if act_comm is not None and not active:
                                pnl = (close_t - entry_px) if is_long else (entry_px - close_t)
                                if pnl >= float(act_comm) * comm:
                                    active = True
                                    act_ts = iso(ctx["tfbars"][tf_i].ts)
                                    tp = None
                            if act_r is not None and act_comm is None and not active:
                                risk = (atr_t or 0.0) * mult
                                move = (peak - entry_px) if is_long else (entry_px - peak)
                                if risk > 0 and move >= float(act_r) * risk:
                                    active = True
                                    act_ts = iso(ctx["tfbars"][tf_i].ts)
                                    tp = None
                            if active and atr_t:
                                stop = _t1_stop(is_long, peak, float(atr_t), dist_atr,
                                                float(close_t), entry_px, mult, compress,
                                                minf, mina, vboost, ratio, stop)
                                stop_final = stop
                exit_side = Side.SELL if is_long else Side.BUY
                ep = hit[1] if hit else float(cl[int(idx[limit - 1])])
                px = cm.fill_price(float(ep), exit_side)
                gross = (px - entry_fill) / entry_fill * (1.0 if is_long else -1.0)
                com = cm.commission_rate
                net = gross - com - com * (px / entry_fill)
                rmult = net / (risk0 / entry_px) if risk0 > 0 else None
                p_i = hit[0] if hit else limit - 1
                exit_reason = hit[2] if hit else "TIMEOUT"
                out_rows.append((
                    sid, code, pid, "", cfg_h,
                    json.dumps(p, ensure_ascii=False, separators=(",", ":")),
                    iso(anchor_ts), entry_px, init_stop, init_tp, bool(active), act_ts,
                    stop_final, iso(bars[int(idx[p_i])].ts), ep, exit_reason,
                    p_i + 1, int(np.ceil((p_i + 1) * 60 / tsec)), gross, net, rmult,
                ))
                res["by_model"][code] = res["by_model"].get(code, 0) + 1
        with eng.begin() as c:
            c.execute(text(
                "DELETE FROM lab_trailing_results t USING lab_signal_events s "
                "WHERE t.signal_id = s.id AND s.run_id = :r AND s.figi = :f"
            ), {"r": args["run_id"], "f": figi})
            drv = c.connection.driver_connection
            cur = drv.cursor()
            try:
                buf = io.StringIO()
                for row in out_rows:
                    buf.write("\t".join("\\N" if v is None else str(v) for v in row) + "\n")
                buf.seek(0)
                cur.copy_expert(
                    f"COPY lab_trailing_results ({', '.join(_COLS)}) FROM STDIN", buf)
            finally:
                cur.close()
        res["rows"] = len(out_rows)
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return res
