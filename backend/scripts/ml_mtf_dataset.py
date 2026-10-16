"""ML-MTF-1 / B1 — датасет «признаки × метки». Версия v3 (REWORK архитектора 2026-10-14).

Что исправлено относительно v2 (v2 отозвана архитектором 2026-10-14):
A. Единая eligibility-policy: ОДИН predicate `_in_session` (будний день +
   НАЧАЛО бара в [10:00, 18:40) MSK ⇒ решение строго < 18:44 MSK) применяется
   И к features (`_continuous_features_1m`), И к разметке (`_session_label_rows`).
   Раньше фильтровалось только время суток, выходные попадали в датасет
   (proof: 37 targets на субботу 2026-08-29). Policy hash — в manifest.
B. Completeness будущего пути: перед low/high КАЖДОЙ будущей 5m-корзины
   проверяется её полнота (n_min5[j] == tf/60). Неполная будущая свеча ⇒ первое
   касание установить нельзя ⇒ CENSORED(reason=future_bucket_incomplete),
   label/end=None. Раньше проверялся только anchor n_min5[i] (proof: свеча из
   1 минуты давала целевой строке MATURE+1).
C. Роли колонок: trainable X = только рыночные функции и голоса
   (`predictor_columns`, 58); `n_min_<tf>`/`voice_status_<sid>`/identity/timing —
   `metadata_columns` (quality metadata, не предикторы).
D. Версии: DATASET_VERSION=ml_mtf_1_v3, FEATURE_SCHEMA_VERSION=1m_mtf_v3, новые
   row_id; params_hash включает eligibility policy + predictor whitelist.

Что было исправлено в v2 (сохранено):
1. Критический сдвиг метки: triple_barrier пропускает прогрев ATR и возвращает
   собственный `index`; compute_ticker v1 шёл по enumerate(lab_rows) и крепил
   метку i-го бара к бару с другим ts (сдвиг 65 минут на непрерывном ряду).
   v2+ использует напрямую bar-index/ts — никакого перечисления сдвинутых строк.
2. label_end_ts = ЗАКРЫТИЕ t1-бара (время, когда исход стал известен), не начало.
   Разметка сессионная: путь ограничен главным окном 10:00–18:40 MSK и идёт по
   РЕАЛЬНОМУ wall-clock 5m-гриду (24 слота = 120 минут), пропуски не сжимаются.
   Ранний подтверждённый hit в ПОЛНОЙ корзине до первого неизвестного/неполного
   слота допустим; после неизвестного/неполного пути первое касание не
   доказывается → CENSORED с reason (session_cutoff / gap / anchor_incomplete /
   future_bucket_incomplete). ATR канонический (indicatorhub), с прошлым
   прогревом, не обнуляется между сессиями.
3. Качество ТФ: bar из неполной корзины (число присутствующих 1m минут < tf/60)
   помечается колонкой n_min_<tf> и НЕ даёт значения OHLC-признаков (ячейки None).
   Дубли _ema/_rsi/_atr14/_macd_hist убраны — используем canonical IndicatorHub
   (app.engine.indicatorhub._atr/_ema/_rsi/_macd/_sma) с их реальным warmup.
4. Голоса: активны только в lookback frozen config (FRESH_MIN=30 мин,
   app.bot.ensemble_strategy): vote_<sid> = действующий голос (0 если протух),
   age_<sid> отдельно. Ошибка источника — voice_status_<sid>="error:<Class>"
   и голос None (missing), а не фиктивный 0.
5. Версии/воспроизводимость: новые row_id/schema; params_hash включает hashes
   входов (manifest snapshot + csv каждого тикера), версии модулей и policy;
   run immutable; resume только при совпадении кода/конфига/входа и проверенной части.
6. Исходная ветка: features на КАЖДОМ eligible закрытом 1m баре (replay),
   supervised targets только на закрытиях 5m (join строго по row_id, 1:1 по
   supervised); метка никогда не приписывается 1m строке другого decision.
   source_inventory.json — read-only инвентаризация БД/parquet (переиспользуется).

Датасет offline: ничего не пишет в БД и не трогает live/execution.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.engine.indicatorhub import _atr, _ema, _macd, _rsi, _sma
from app.engine.labeling import BarrierConfig
from app.engine.models import Candle as EngineCandle
from app.services.ensemble import compute_bias, resample
from app.services.signals import generate_signals

UTC = timezone.utc
MSK = ZoneInfo("Europe/Moscow")

# ---- контракт v3: фиксированные параметры (не подбираются по результату) ----
DATASET_VERSION = "ml_mtf_1_v3"
FEATURE_SCHEMA_VERSION = "1m_mtf_v3"
LABEL_CFG = BarrierConfig(take_atr=1.5, stop_atr=1.5, max_bars=24, atr_period=14)
SUPERVISED_TF = 300            # 5m — бары, на которых живут supervised-метки
TF_SECONDS = (60, 300, 600, 1800, 3600)   # 1/5/10/30/60 минут
BIAS_TF = 3600                 # bias по часовым (compute_bias, period=50)
BIAS_PERIOD = 50
VOLR_WINDOW = 20
WARMUP_DAYS = 10          # прогрев m1 до start (буферы не холодные)

# Российский воксель сессии (frozen branch-окно, синхронизировано с B3):
# главная сессия 10:00–18:44 MSK; 5m-бар принадлежит сессии, если его НАЧАЛО
# в [10:00, 18:40) MSK (последний закрывается в 18:40 MSK, вход строго до 18:44).
SESSION_TZ = "Europe/Moscow"
SESSION_OPEN = "10:00"
SESSION_CLOSE = "18:40"   # эксклюзив по началу бара: бары 10:00..18:39:59 MSK
_S_OPEN = dtime(10, 0)
_S_CLOSE = dtime(18, 40)
_WEEKDAYS = (0, 1, 2, 3, 4)   # пн–пт: явное ограничение исследования этой ветки

# ЕДИНАЯ eligibility-policy (один predicate для features И labels, REWORK v3 п.1):
# будний день + НАЧАЛО бара в [10:00, 18:40) MSK ⇒ решение (close бара) строго < 18:44 MSK.
# Строки выходных НЕ удаляются из БД и не объявляются ошибочными рыночными данными —
# они лишь не являются eligible-решениями этой ветки.
ELIGIBILITY = {
    "tz": SESSION_TZ, "weekdays": [0, 1, 2, 3, 4],
    "open": SESSION_OPEN, "close": SESSION_CLOSE, "decision_strictly_before": "18:44",
}


def _eligibility_hash():
    return hashlib.sha256(json.dumps(ELIGIBILITY, sort_keys=True).encode()).hexdigest()[:16]


ELIGIBILITY_POLICY_HASH = _eligibility_hash()


def _in_session(ts):
    """Единый eligibility-predicate: будний день и НАЧАЛО бара в [10:00, 18:40) MSK."""
    local = ts.astimezone(MSK)
    return local.weekday() in _WEEKDAYS and _S_OPEN <= local.time() < _S_CLOSE

# SETUP-источники голосов — те же, что в ensemble (9 голосов + quorum)
VOICE_SETUPS = (
    ("rsi_reversal", 300, {"period": 14, "oversold": 35.0, "overbought": 65.0}),
    ("macd_cross", 300, {"fast": 12, "slow": 26, "signal_period": 9}),
    ("range_compression_breakout", 300, {"lookback": 20, "atr_period": 14, "pct": 25.0}),
    ("pullback_ema", 300, {"trend_ema": 50, "pull_ema": 20}),
    ("vwap_reclaim", 300, {}),
    ("vwap_reversion", 300, {}),
    ("donchian_breakout", 300, {}),
    ("volume_drop", 300, {}),
    ("kama_trend", 300, {}),
)
QUORUM = 2
# Голос действует только внутри lookback frozen ensemble config (FRESH_MIN=30,
# app/bot/ensemble_strategy.py). age сохраняется отдельно от active vote.
VOICE_FRESH_MIN = 30

RSI_P, MACD_F, MACD_S, MACD_SIG = 14, 12, 26, 9
EMA_P, ATR_P = 50, 14

# непрерывные признаки (по всем ТФ) — канонические серии IndicatorHub
CONTINUOUS_PREFIXES = ("rsi14", "macd_hist", "macd_hist_delta", "ema50_dist",
                       "ema50_slope", "atr14_price", "volr20")


def _tf_suffix(tf):
    return "1m" if tf == 60 else ("5m" if tf == SUPERVISED_TF else f"{tf}s")


def _label_cfg_hash():
    payload = {
        "take": LABEL_CFG.take_atr, "stop": LABEL_CFG.stop_atr,
        "max_bars": LABEL_CFG.max_bars, "atr_period": LABEL_CFG.atr_period,
        "tf": SUPERVISED_TF, "session": {"tz": SESSION_TZ, "open": SESSION_OPEN,
                                         "close": SESSION_CLOSE},
        "wall_clock": True,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


LABEL_CFG_HASH = _label_cfg_hash()


# ---------------------------------------------------------------- загрузка
def _iso_bound(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.replace(microsecond=0).isoformat()


def load_m1(csv_path, figi, ticker, start, end):
    """CSV 1m → list[EngineCandle], только closed бары в [start, end).

    Быстрый путь через pyarrow.csv; отбор по ts строковым сравнением (ISO с
    +00:00 сравнивается лексикографически), парсинг только попавших строк."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.csv as pacsv

    tbl = pacsv.read_csv(
        csv_path,
        convert_options=pacsv.ConvertOptions(
            include_columns=["figi", "ts", "open", "high", "low", "close", "volume"],
            column_types={"figi": pa.string(), "ts": pa.string()}),
    )
    if tbl.num_rows == 0:
        return []
    if figi:
        mask = pc.equal(tbl.column("figi"), pa.scalar(figi))
        tbl = tbl.filter(mask)
        if tbl.num_rows == 0:
            return []
    s_iso, e_iso = _iso_bound(start), _iso_bound(end)
    ts_col = tbl.column("ts")
    mask = pc.and_(pc.greater_equal(ts_col, pa.scalar(s_iso)),
                   pc.less(ts_col, pa.scalar(e_iso)))
    tbl = tbl.filter(mask)
    if tbl.num_rows == 0:
        return []
    d = tbl.to_pydict()
    out = []
    for i in range(tbl.num_rows):
        ts = datetime.fromisoformat(d["ts"][i])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        try:
            out.append(EngineCandle(
                ts=ts, open=float(d["open"][i]), high=float(d["high"][i]),
                low=float(d["low"][i]), close=float(d["close"][i]),
                volume=float(d["volume"][i])))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda x: x.ts)
    return out


def _resample_all(m1):
    """1m → все ТФ. Иерархически от 5m (300 делит 600/1800/3600) — тот же
    результат, что прямой resample(m1, tf), без повторного прохода по 1m."""
    bars5 = resample(m1, SUPERVISED_TF)
    out = {60: m1, SUPERVISED_TF: bars5}
    for tf in TF_SECONDS:
        if tf not in out:
            out[tf] = resample(bars5, tf)
    return out


def _bucket_minutes(m1, tf_bars, tf):
    """Число присутствующих 1m минут в корзине каждого tf-бара ([start, start+tf)).

    Это sparse/coverage-контракт: bar из неполной корзины не даёт доверенных
    high/low/close (и объёма), его ячейки → None, а n_min сохраняет факт."""
    if tf == 60:
        return [1] * len(tf_bars)
    ts = [int(c.ts.timestamp()) for c in m1]
    out = []
    idx = 0
    for b in tf_bars:
        lo = int(b.ts.timestamp())
        hi = lo + tf
        si = bisect_left(ts, lo, idx)
        ei = bisect_left(ts, hi, si)
        out.append(ei - si)
        idx = si
    return out


# ------------------------------------------------------- канонические индикаторы
def _ind_series(bars, tf, m1):
    """Все серии одного ТФ РОВНО ОДИН РАЗ через canonical IndicatorHub."""
    closes = [b.close for b in bars]
    hist = _macd(bars, MACD_F, MACD_S, MACD_SIG)["histogram"]
    return {
        "bars": bars,
        "ends": [b.ts + timedelta(seconds=tf) for b in bars],
        "rsi": _rsi(bars, RSI_P),
        "hist": hist,
        "ema": _ema(closes, EMA_P),
        "ema_prev": None,  # вычисляется на лету по индексу
        "atr": _atr(bars, ATR_P),
        "vol": [b.volume for b in bars],
        "volsma": _sma([b.volume for b in bars], VOLR_WINDOW),
        "n_min": _bucket_minutes(m1, bars, tf),
    }


def _cells_at(ind, j, tf):
    """7 непрерывных ячеек одного ТФ на баре j (+n_min корзины)."""
    suf = _tf_suffix(tf)
    expected = max(1, tf // 60)
    n_min = ind["n_min"][j] if j < len(ind["n_min"]) else 0
    complete = (tf == 60) or (n_min >= expected)
    empty = {f"{name}_{suf}": None for name in CONTINUOUS_PREFIXES}
    if not complete:
        return {"cells": empty, "n_min": n_min}
    bj = ind["bars"][j]
    close = bj.close
    hist = ind["hist"][j]
    hist_p = ind["hist"][j - 1] if j > 0 else None
    ema = ind["ema"][j]
    ema_p = ind["ema"][j - 1] if j > 0 else None
    atr = ind["atr"][j]
    vsma = ind["volsma"][j]
    vol = ind["vol"][j] if j < len(ind["vol"]) else None
    out = {}
    out[f"rsi14_{suf}"] = ind["rsi"][j]
    out[f"macd_hist_{suf}"] = hist
    out[f"macd_hist_delta_{suf}"] = (
        hist - hist_p if hist is not None and hist_p is not None else None)
    out[f"ema50_dist_{suf}"] = (
        (close - ema) / close if ema is not None and close else None)
    out[f"ema50_slope_{suf}"] = (
        (ema - ema_p) / close if ema is not None and ema_p is not None and close else None)
    out[f"atr14_price_{suf}"] = (atr / close if atr is not None and close else None)
    out[f"volr20_{suf}"] = (vol / vsma if vsma is not None and vsma > 0 and vol is not None
                            else None)
    return {"cells": out, "n_min": n_min}


# ---------------------------------------------------------------- голоса
def _voice_index(bars_by_tf):
    """{sid: (sorted_avail_ts, sides, status)} по ЗАКРЫТЫМ барам своего ТФ.

    generate_signals отдаёт signal с ts = НАЧАЛО бара → доступен в ts + tf.
    Ошибка функции фиксируется (status="error:<Class>") и НЕ маскируется под
    «сигналов нет»: голос будет None (missing), а не фиктивный 0."""
    out = {}
    for sid, tf, params in VOICE_SETUPS:
        try:
            sigs = generate_signals(sid, params, bars_by_tf[tf])
            status = "ok"
        except Exception as exc:  # noqa: BLE001 — статус источника обязателен
            sigs = []
            status = f"error:{type(exc).__name__}"
        avail = {}
        for sg in sigs or []:
            s = str(sg.get("side", "")).upper()
            side = 1 if "BUY" in s else (-1 if "SELL" in s else 0)
            if not side:
                continue
            ts = sg.get("ts")
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            avail[ts + timedelta(seconds=tf)] = side
        ordered = sorted(avail.items())
        out[sid] = ([t for t, _ in ordered], [s for _, s in ordered], status)
    return out


def _vote_at(vidx, sid, decision_ts):
    """(active_vote, age_min, status): активен только в lookback VOICE_FRESH_MIN."""
    ts_list, sides, status = vidx.get(sid, ([], [], "ok"))
    k = bisect_right(ts_list, decision_ts) - 1
    if k < 0:
        return 0, None, status
    age = (decision_ts - ts_list[k]).total_seconds() / 60.0
    side = sides[k]
    active = side if age <= VOICE_FRESH_MIN else 0
    return active, age, status


# ---------------------------------------------------------------- разметка
def _session_label_rows(bars5, m1, start, end):
    """Сессионная triple-barrier разметка на закрытых 5m барах (REWORK v3 пп.1-2).

    - eligible-бар: `_in_session` (будний день, НАЧАЛО в [10:00, 18:40) MSK) —
      ОДИН predicate с features;
    - горизонт: 24 слота по РЕАЛЬНОМУ 5m-гриду (wall-clock 120 минут).
      КАЖДАЯ будущая корзина проверяется на полноту ДО low/high: неполный
      будущий bucket ⇒ нельзя установить первое касание ⇒
      CENSORED(future_bucket_incomplete), label/end=None;
    - отсутствующий слот = неизвестный путь: до первого касания → CENSORED(gap),
      за границей сессии → CENSORED(session_cutoff), неполная корзина решения →
      CENSORED(anchor_incomplete);
    - ранний подтверждённый hit в ПОЛНОЙ корзине до первого неизвестного/неполного
      слота допустим и MATURE (включая timeout только после 24 полных слотов);
    - ATR канонический indicatorhub._atr по всей серии (прогрев через пропуски);
    - label_end_ts = ЗАКРЫТИЕ t1-бара (время известности исхода)."""
    atr = _atr(bars5, ATR_P)
    n_min5 = _bucket_minutes(m1, bars5, SUPERVISED_TF)
    need_min = SUPERVISED_TF // 60
    n = len(bars5)
    by_start, in_window = {}, {}
    for i, b in enumerate(bars5):
        if _in_session(b.ts):
            key = b.ts.astimezone(MSK).date()
            by_start.setdefault(key, {})[b.ts] = i
            in_window[i] = key
    rows = []
    for i in range(n):
        if i not in in_window:
            continue
        a = atr[i]
        if a is None or a <= 0:      # канонический прогрев ATR, индекс не сдвигает
            continue
        decision_ts = bars5[i].ts + timedelta(seconds=SUPERVISED_TF)
        if decision_ts < start or decision_ts >= end:
            continue
        if n_min5[i] < need_min:
            rows.append(_target_row(bars5, i, decision_ts,
                                    label=None, status="CENSORED",
                                    reason="anchor_incomplete", a=a))
            continue
        key = in_window[i]
        smap = by_start[key]
        c = bars5[i]
        upper = c.close + LABEL_CFG.take_atr * a
        lower = c.close - LABEL_CFG.stop_atr * a
        label, t1, hit, reason = 0, None, False, None
        for k in range(1, LABEL_CFG.max_bars + 1):
            slot = decision_ts + timedelta(seconds=SUPERVISED_TF * (k - 1))
            j = smap.get(slot)
            if j is None:
                slot_local = slot.astimezone(MSK)
                if (slot_local.date() != decision_ts.astimezone(MSK).date()
                        or slot_local.time() >= _S_CLOSE):
                    reason, label, t1, hit = "session_cutoff", 0, None, False
                else:
                    reason, label, t1, hit = "gap", 0, None, False
                break
            if n_min5[j] < need_min:
                # неполная будущая 5m-свеча: путь внутри слота неизвестен,
                # первое касание установить нельзя (даже если low/high её задели)
                reason, label, t1, hit = "future_bucket_incomplete", 0, None, False
                break
            jb = bars5[j]
            dn = jb.low <= lower
            up = jb.high >= upper
            if dn and up:            # double-touch → stop-first (как движок)
                label, t1, hit = -1, j, True
                break
            if dn:
                label, t1, hit = -1, j, True
                break
            if up:
                label, t1, hit = 1, j, True
                break
        else:
            # все 24 слота полны и без касания → time-barrier 0
            label, t1, hit = 0, i + LABEL_CFG.max_bars, True
        status = "MATURE" if hit else "CENSORED"
        rows.append(_target_row(bars5, i, decision_ts,
                                label=label if status == "MATURE" else None,
                                status=status, reason=reason if status == "CENSORED" else None,
                                t1=t1, a=a))
    return rows


def _target_row(bars5, i, decision_ts, label, status, reason, a, t1=None):
    t1_ts = label_end = None
    if t1 is not None:
        t1_ts = bars5[t1].ts.isoformat()
        label_end = (bars5[t1].ts + timedelta(seconds=SUPERVISED_TF)).isoformat()
    return {
        "index": i,
        "ts": bars5[i].ts.isoformat(),
        "decision_ts": decision_ts.isoformat(),
        "label": label,
        "label_status": status,
        "reason": reason,
        "t1_index": t1,
        "t1_ts": t1_ts,
        "label_end_ts": label_end,
        "upper": round(bars5[i].close + LABEL_CFG.take_atr * a, 6),
        "lower": round(bars5[i].close - LABEL_CFG.stop_atr * a, 6),
        "entry_px": bars5[i].close,
    }


# ---------------------------------------------------------------- features
def _continuous_features_1m(m1, bars_by_tf, vidx, start, end):
    """Признаки на КАЖДОМ закрытом 1m баре внутри главной сессии (closed-only).

    Eligible-решения задаёт ЕДИНЫЙ predicate `_in_session` (будний день +
    НАЧАЛО в [10:00, 18:40) MSK), тот же, что в разметке. Для каждого решения
    (1m close) берём последний ЗАКРЫТЫЙ бар каждого ТФ (END <= decision_ts),
    forming bars запрещены; неполная корзина → None+n_min."""
    ind = {tf: _ind_series(bars_by_tf[tf], tf, m1) for tf in TF_SECONDS}
    ends = {tf: ind[tf]["ends"] for tf in TF_SECONDS}
    bias_map = compute_bias(bars_by_tf[BIAS_TF], BIAS_PERIOD, BIAS_TF)
    cell_cache: dict = {}
    rows = []
    for i, b in enumerate(m1):
        if not _in_session(b.ts):
            continue
        decision_ts = b.ts + timedelta(seconds=60)
        if decision_ts < start or decision_ts >= end:
            continue
        row = {}
        for tf in TF_SECONDS:
            if tf == 60:
                j = i
            else:
                j = bisect_right(ends[tf], decision_ts) - 1
            if j < 0:
                for name in CONTINUOUS_PREFIXES:
                    row[f"{name}_{_tf_suffix(tf)}"] = None
                row[f"n_min_{_tf_suffix(tf)}"] = 0
                continue
            key = (tf, j)
            cells = cell_cache.get(key)
            if cells is None:
                cells = _cells_at(ind[tf], j, tf)
                cell_cache[key] = cells
            row.update(cells["cells"])
            row[f"n_min_{_tf_suffix(tf)}"] = cells["n_min"]
        row["bias"] = bias_map.get(int(decision_ts.timestamp()) // BIAS_TF)
        n_buy = n_sell = 0
        for sid, _, _ in VOICE_SETUPS:
            vote, age, status = _vote_at(vidx, sid, decision_ts)
            row[f"vote_{sid}"] = vote if status == "ok" else None
            row[f"age_{sid}"] = age if status == "ok" else None
            row[f"voice_status_{sid}"] = status
            if vote == 1:
                n_buy += 1
            elif vote == -1:
                n_sell += 1
        row["voice_buy"] = n_buy
        row["voice_sell"] = n_sell
        row["voice_quorum"] = 1 if max(n_buy, n_sell) >= QUORUM else 0
        row["voice_conflict"] = 1 if (n_buy > 0 and n_sell > 0) else 0
        rows.append((decision_ts, row))
    return rows


# ---------------------------------------------------------------- датасет
def _feat_row_id(figi, decision_ts, version):
    key = f"{figi}|{decision_ts.isoformat()}|{version}"
    return hashlib.sha256(key.encode()).hexdigest()


def compute_ticker(csv_path, figi, ticker, start, end):
    """Один тикер → (feature_rows_1m, target_rows_5m). Closed-only, без будущего.

    Прогрев: m1 грузится за WARMUP_DAYS до `start` (буферы не холодные); строки
    отдаются только с decision_ts >= start. features — на каждом 1m баре,
    targets — только на закрытиях 5m; row_id общий (join 1:1 по supervised)."""
    pre = start - timedelta(days=WARMUP_DAYS)
    m1 = load_m1(csv_path, figi, ticker, pre, end)
    if len(m1) < ATR_P + 2:
        return [], [], 0
    bars_by_tf = _resample_all(m1)
    vidx = _voice_index(bars_by_tf)
    feat_rows = _continuous_features_1m(m1, bars_by_tf, vidx, start, end)
    frows = []
    f_ts = set()
    for decision_ts, row in feat_rows:
        f_ts.add(decision_ts)
        fr = {
            "row_id": _feat_row_id(figi, decision_ts, DATASET_VERSION),
            "figi": figi, "ticker": ticker,
            "decision_ts": decision_ts.isoformat(),
            "feature_max_available_ts": decision_ts.isoformat(),
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
        }
        fr.update(row)
        frows.append(fr)
    trows = []
    skipped_no_1m = 0
    for lr in _session_label_rows(bars_by_tf[SUPERVISED_TF], m1, start, end):
        decision_ts = datetime.fromisoformat(lr["decision_ts"])
        if decision_ts not in f_ts:
            # Закрывающая 1m минута якоря отсутствует в ряду (sparse gap):
            # решения на этой минуте нет — строку не эмитируем, факт в diag.
            skipped_no_1m += 1
            continue
        trows.append({
            "row_id": _feat_row_id(figi, decision_ts, DATASET_VERSION),
            "figi": figi, "ticker": ticker,
            "decision_ts": lr["decision_ts"],
            "label": lr["label"],
            "label_status": lr["label_status"],
            "label_cfg_hash": LABEL_CFG_HASH,
            "label_end_ts": lr["label_end_ts"],
            "label_status_reason": lr["reason"],
        })
    return frows, trows, skipped_no_1m


# ------------------------------------------------- фиксированные схемы
_TARGET_COLUMNS = [("row_id", "string"), ("figi", "string"), ("ticker", "string"),
                   ("decision_ts", "string"), ("label", "int64"),
                   ("label_status", "string"), ("label_cfg_hash", "string"),
                   ("label_end_ts", "string"), ("label_status_reason", "string")]


def _feature_columns():
    """Фиксированный порядок/типы колонок features — одинаков во всех партах."""
    cols = [("row_id", "string"), ("figi", "string"), ("ticker", "string"),
            ("decision_ts", "string"), ("feature_max_available_ts", "string"),
            ("feature_schema_version", "string")]
    for tf in TF_SECONDS:
        suf = _tf_suffix(tf)
        for name in CONTINUOUS_PREFIXES:
            cols.append((f"{name}_{suf}", "float64"))
        cols.append((f"n_min_{suf}", "int64"))
    cols.append(("bias", "int64"))
    for sid, _, _ in VOICE_SETUPS:
        cols.append((f"vote_{sid}", "int64"))
        cols.append((f"age_{sid}", "float64"))
        cols.append((f"voice_status_{sid}", "string"))
    cols += [("voice_buy", "int64"), ("voice_sell", "int64"),
             ("voice_quorum", "int64"), ("voice_conflict", "int64")]
    return cols


# trainable X = ТОЛЬКО рыночные функции и голоса (REWORK v3 п.3); n_min/voice_status/
# identity/timing остаются quality metadata для coverage и eligible decisions —
# их нельзя включать в предикторы через suffix-fallback.
_METADATA_COLUMNS = (["row_id", "figi", "ticker", "decision_ts",
                      "feature_max_available_ts", "feature_schema_version"]
                     + [f"n_min_{_tf_suffix(tf)}" for tf in TF_SECONDS]
                     + [f"voice_status_{sid}" for sid, _, _ in VOICE_SETUPS])


def _predictor_columns():
    """Whitelist предикторов: 35 рыночных непрерывных + bias + 9 vote_ + 9 age_ + 4 агрегата."""
    cols = []
    for tf in TF_SECONDS:
        suf = _tf_suffix(tf)
        cols += [f"{name}_{suf}" for name in CONTINUOUS_PREFIXES]
    cols.append("bias")
    for sid, _, _ in VOICE_SETUPS:
        cols += [f"vote_{sid}", f"age_{sid}"]
    cols += ["voice_buy", "voice_sell", "voice_quorum", "voice_conflict"]
    return cols


def _column_roles():
    pred = set(_predictor_columns())
    return {n: ("predictor" if n in pred else "metadata")
            for n, _ in _feature_columns()}


def _pa_schema(cols):
    import pyarrow as pa
    m = {"string": pa.string(), "float64": pa.float64(), "int64": pa.int64()}
    return pa.schema([(n, m[t]) for n, t in cols])


def _part_paths(out_dir, ticker):
    return (out_dir / "features" / f"features_{ticker}.parquet",
            out_dir / "targets" / f"targets_{ticker}.parquet",
            out_dir / "meta" / f"meta_{ticker}.json")


def _write_part(out_dir, ticker, frows, trows, params_hash, csv_sha,
                skipped_no_1m_anchor=0):
    import pyarrow as pa
    import pyarrow.parquet as pq
    fp, tp, mp = _part_paths(out_dir, ticker)
    fp.parent.mkdir(parents=True, exist_ok=True)
    tp.parent.mkdir(parents=True, exist_ok=True)
    mp.parent.mkdir(parents=True, exist_ok=True)
    fsch, tsch = _pa_schema(_feature_columns()), _pa_schema(_TARGET_COLUMNS)
    pq.write_table(pa.Table.from_pylist(frows, schema=fsch), fp)
    pq.write_table(pa.Table.from_pylist(trows, schema=tsch), tp)
    reasons = {}
    for r in trows:
        if r["label_status"] == "CENSORED":
            reasons[r["label_status_reason"]] = reasons.get(r["label_status_reason"], 0) + 1
    dts = [r["decision_ts"] for r in frows]
    status = {"status": "ok" if frows else "no_bars",
              "feature_rows": len(frows), "target_rows": len(trows),
              "supervised": sum(1 for r in trows if r["label_status"] == "MATURE"),
              "censored": sum(1 for r in trows if r["label_status"] == "CENSORED"),
              "censored_reasons": reasons,
              "skipped_no_1m_anchor": skipped_no_1m_anchor,
              "min_ts": min(dts) if dts else None,
              "max_ts": max(dts) if dts else None,
              "params_hash": params_hash, "csv_sha256": csv_sha}
    mp.write_text(json.dumps(status, indent=1))
    return status


def _scan_universe(snapshot_dir, tickers):
    """ticker -> (figi, csv_path) из manifest snapshot; отсутствующие отражаются."""
    mpath = snapshot_dir / "manifest.json"
    uni = {}
    if mpath.exists():
        man = json.loads(mpath.read_text())
        for t, e in man.get("per_ticker", {}).items():
            uni[t] = (e.get("figi", ""), snapshot_dir / "csv" / f"{t}__m1.csv")
    else:
        for p in sorted((snapshot_dir / "csv").glob("*__m1.csv")):
            t = p.name.split("__")[0]
            uni[t] = ("", p)
    return {t: uni[t] for t in tickers if t in uni}


# ------------------------------------------------- source_inventory (read-only)
def _write_source_inventory(out, snapshot):
    """Инвентаризация существующих artifacts (read-only): старые parquet +
    таблицы БД (counts/schema). Изменять БД нельзя; недоступность фиксируем."""
    import pyarrow.parquet as pq
    inv = {"old_parquet": {}, "db_tables": {}, "reuse_rationale": {}}
    old = [Path("reports/label_votes.parquet"), Path("reports/labeled_dataset.parquet"),
           Path("reports/labeled_8m.parquet")]
    for rel in old:
        p = (Path(__file__).resolve().parents[1] / rel)
        rec = {"path": str(p), "exists": p.exists()}
        if p.exists():
            try:
                sch = pq.read_schema(p)
                rec["columns"] = [f"{f.name}:{f.type}" for f in sch]
                rec["n_rows"] = pq.ParquetFile(p).metadata.num_rows
            except Exception as exc:  # noqa: BLE001
                rec["read_error"] = f"{type(exc).__name__}: {exc}"
        inv["old_parquet"][rel.name] = rec
    # read-only БД-инвентаризация
    inv["db_tables"] = _db_inventory()
    inv["reuse_rationale"] = {
        "labels/ideal_trades (БД)": "старые labels не сессионные и не wall-clock "
                                    "(REWORK п.2); cfg/семантика несовместимы — "
                                    "пересчёт новым сессионным ядром offline",
        "reports/label_votes.parquet": "БЕЗ provenance; label_votes.py выбирал upper "
                                       "перед lower при двойном касании (расхождение "
                                       "с canonical stop-first) — не канонические",
        "reports/labeled_*.parquet": "связаны со старым сигнальным скриптом, тайминг "
                                     "X↔y не проверялся — не использовать",
    }
    inv["snapshot"] = {"path": str(snapshot),
                       "manifest_sha256": _sha_of(snapshot / "manifest.json")}
    (out / "source_inventory.json").write_text(json.dumps(inv, indent=1, default=str))


def _sha_of(path):
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _db_inventory():
    """Read-only counts по labels/ideal_trades/signals/strategy_runs/trace/outcomes."""
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        envp = Path(__file__).resolve().parents[1] / ".env"
        if envp.exists():
            for line in envp.read_text().splitlines():
                if line.startswith("DATABASE_URL="):
                    dsn = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not dsn:
        return {"note": "DATABASE_URL не найден — БД-инвентаризация пропущена"}
    import asyncio
    tables = ["labels", "ideal_trades", "signals", "strategy_runs",
              "signal_trace_runs", "signal_trace_events", "signal_trace_outcomes",
              "signal_decisions"]
    result = {"dsn_host": "", "note": ""}

    async def _run():
        import asyncpg
        try:
            conn = await asyncpg.connect(dsn, timeout=8,
                                         server_settings={"statement_timeout": "15000"})
        except Exception as exc:  # noqa: BLE001
            return {"note": f"connection error: {type(exc).__name__}"}
        try:
            info = await conn.fetch(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema='public'")
            colmap = {}
            for r in info:
                colmap.setdefault(r["table_name"], []).append(r["column_name"])
            for t in tables:
                try:
                    cnt = await conn.fetchval(f'SELECT count(*) FROM "{t}"')
                    c = colmap.get(t, [])
                    rec = {"count": cnt, "columns": c}
                    ts_col = "ts" if "ts" in c else ("entry_ts" if "entry_ts" in c
                                                     else "created_at" if "created_at" in c else None)
                    if ts_col:
                        mn, mx = await conn.fetchrow(
                            f'SELECT min("{ts_col}"), max("{ts_col}") FROM "{t}"')
                        rec["min_ts"] = str(mn) if mn else None
                        rec["max_ts"] = str(mx) if mx else None
                    result[t] = rec
                except Exception as exc:  # noqa: BLE001
                    result[t] = {"error": f"{type(exc).__name__}"}
            return result
        finally:
            await conn.close()

    try:
        return asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        return {"note": f"db inventory error: {type(exc).__name__}: {exc}"}


# ------------------------------------------------- воспроизводимость
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_MODULES = ("backend/scripts/ml_mtf_dataset.py",
            "backend/app/engine/labeling.py",
            "backend/app/engine/indicatorhub.py",
            "backend/app/services/ensemble.py",
            "backend/app/services/signals.py")


def _module_shas():
    return {p: _sha_of(_BACKEND_ROOT / p) for p in _MODULES}


def build_params_hash(snapshot, start, end, code_sha):
    mani_sha = _sha_of(snapshot / "manifest.json")
    return hashlib.sha256(json.dumps({
        "code_sha256": code_sha,
        "modules": _module_shas(),
        "input_manifest_sha256": mani_sha,
        "start": start.isoformat(), "end": end.isoformat(),
        "dataset_version": DATASET_VERSION,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "label_cfg_hash": LABEL_CFG_HASH,
        "eligibility_policy": ELIGIBILITY,
        "eligibility_policy_hash": ELIGIBILITY_POLICY_HASH,
        "predictor_columns": _predictor_columns(),
        "voice_setups": VOICE_SETUPS, "voice_fresh_min": VOICE_FRESH_MIN,
        "session": {"tz": SESSION_TZ, "open": SESSION_OPEN, "close": SESSION_CLOSE},
        "warmup_days": WARMUP_DAYS, "quorum": QUORUM,
        "bias": {"tf": BIAS_TF, "period": BIAS_PERIOD},
        "volr_window": VOLR_WINDOW,
    }, sort_keys=True, default=str).encode()).hexdigest()


# ------------------------------------------------- validation
def validate_run(out):
    """Проверка готового run: join 1:1 supervised, схема=manifest, статусы,
    label_end_ts > decision_ts, отсутствие дублей row_id. read-only."""
    import pyarrow.parquet as pq
    man = json.loads((out / "manifest.json").read_text())
    fcols = [c["name"] for c in man["feature_columns"]]
    tcols = [c["name"] for c in man["target_columns"]]
    diag = {"parts": {}, "errors": [], "warnings": []}
    for part in man["parts"]:
        t = part["ticker"]
        fp = out / part["features"]
        tp = out / part["targets"]
        if not (fp.exists() and tp.exists()):
            diag["errors"].append(f"{t}: отсутствует часть")
            continue
        fd = pq.read_table(fp).to_pydict()
        td = pq.read_table(tp).to_pydict()
        fid = set(fd["row_id"])
        tid = set(td["row_id"])
        dup_f = int(len(fd["row_id"]) - len(fid))
        dup_t = int(len(td["row_id"]) - len(tid))
        only_in_t = len(tid - fid)
        if dup_f or dup_t:
            diag["errors"].append(f"{t}: дубли row_id f={dup_f} t={dup_t}")
        if only_in_t:
            diag["errors"].append(f"{t}: только в targets {only_in_t}")
        lst = set(td["label_status"])
        if not lst <= {"MATURE", "CENSORED"}:
            diag["errors"].append(f"{t}: label_status={lst}")
        for lab, st in zip(td["label"], td["label_status"]):
            if (st == "MATURE" and lab is None) or (st == "CENSORED" and lab is not None):
                diag["errors"].append(f"{t}: label/status не согласованы")
                break
        bad_end = 0
        for dt, le, st in zip(td["decision_ts"], td["label_end_ts"], td["label_status"]):
            if st == "MATURE":
                d0, e0 = datetime.fromisoformat(dt), datetime.fromisoformat(le)
                if le is None or e0 <= d0 or (e0 - d0).total_seconds() % 300 != 0:
                    bad_end += 1
        if bad_end:
            diag["errors"].append(f"{t}: label_end_ts некорректны {bad_end}")
        # eligibility: решения только в будни (policy hash фиксирует и окно)
        wk_f = sum(1 for s in fd["decision_ts"]
                   if datetime.fromisoformat(s).astimezone(MSK).weekday() > 4)
        wk_t = sum(1 for s in td["decision_ts"]
                   if datetime.fromisoformat(s).astimezone(MSK).weekday() > 4)
        diag["weekday_violations"] = diag.get("weekday_violations", 0) + wk_f + wk_t
        if wk_f or wk_t:
            diag["errors"].append(f"{t}: weekend decisions f={wk_f} t={wk_t}")
        # reason: CENSORED ⇒ непустая причина из allowed; MATURE ⇒ причины нет
        allowed = set(man.get("label_status_reason_values", []))
        bad_reason = 0
        for st, rsn in zip(td["label_status"], td["label_status_reason"]):
            if st == "CENSORED":
                if rsn is None or (allowed and rsn not in allowed):
                    bad_reason += 1
            elif rsn is not None:
                bad_reason += 1
        if bad_reason:
            diag["errors"].append(f"{t}: label_status_reason некорректны {bad_reason}")
        n_fb = sum(1 for rsn in td["label_status_reason"]
                   if rsn == "future_bucket_incomplete")
        if n_fb:
            diag.setdefault("future_bucket_incomplete", {})[t] = n_fb
        diag["parts"][t] = {"feature_rows": len(fd["row_id"]),
                            "target_rows": len(td["row_id"]),
                            "supervised": sum(1 for s in td["label_status"] if s == "MATURE"),
                            "censored": sum(1 for s in td["label_status"] if s == "CENSORED")}
    # схема партиций == manifest
    if "label" in fcols:
        diag["errors"].append("features содержит 'label' (запрещено)")
    for key in ("predictor_columns", "metadata_columns"):
        if not man.get(key):
            diag["warnings"].append(f"manifest не декларирует {key}")
    with pq.ParquetFile(out / man["parts"][0]["features"]) as pf:
        if pf.schema.names != fcols:
            diag["errors"].append("features-схема расходится с manifest")
    with pq.ParquetFile(out / man["parts"][0]["targets"]) as pf:
        if pf.schema.names != tcols:
            diag["errors"].append("targets-схема расходится с manifest")
    diag["ok"] = not diag["errors"]
    return diag


# ------------------------------------------------- main
def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="ML-MTF-1 dataset builder (B1, v3)")
    ap.add_argument("--snapshot", required=True, type=Path)
    ap.add_argument("--start", required=True, help="ISO начало [включительно]")
    ap.add_argument("--end", required=True, help="ISO конец [исключительно], "
                                                "2026-10-09 включает 08.10")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--tickers", nargs="*", default=None)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="перезаписать run с другим params_hash (не рекомендуется)")
    ap.add_argument("--inventory", action="store_true",
                    help="source_inventory.json: read-only инвентаризация БД/parquet")
    ap.add_argument("--validate", action="store_true",
                    help="проверить готовый run и выйти")
    args = ap.parse_args(argv)

    if args.validate:
        diag = validate_run(args.out)
        print(json.dumps(diag, indent=1, default=str))
        return diag

    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)

    if args.tickers:
        tickers = list(args.tickers)
    else:
        mpath = args.snapshot / "manifest.json"
        tickers = sorted(json.loads(mpath.read_text())["per_ticker"].keys())

    code_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    params_hash = build_params_hash(args.snapshot, start, end, code_sha)

    # immutable: существующий run другой идентичности не перезаписываем
    man_path = out / "manifest.json"
    if man_path.exists() and not args.force:
        prev = json.loads(man_path.read_text()).get("params_hash")
        if prev != params_hash and not args.resume:
            raise SystemExit(
                f"{out}: manifest существует с params_hash={prev} != {params_hash}\n"
                "используйте новый --out, --resume (пропустить совпавшие части) "
                "или --force")

    if args.inventory:
        _write_source_inventory(out, args.snapshot)

    uni = _scan_universe(args.snapshot, tickers)
    per_ticker = {}
    for t in tickers:
        if t not in uni:
            per_ticker[t] = {"status": "not_in_universe"}
            continue
        figi, csvp = uni[t]
        _, _, mp = _part_paths(out, t)
        csv_sha = _sha_of(csvp)
        if args.resume and mp.exists():
            prev = json.loads(mp.read_text())
            if (prev.get("params_hash") == params_hash
                    and prev.get("csv_sha256") == csv_sha
                    and prev.get("status") == "ok"):
                per_ticker[t] = prev
                print(f"{t}: resume skip ({prev.get('feature_rows')} rows)")
                continue
        if not csvp.exists():
            per_ticker[t] = {"status": "no_csv", "csv": str(csvp)}
            print(f"{t}: no_csv")
            continue
        frows, trows, skipped = compute_ticker(csvp, figi, t, start, end)
        per_ticker[t] = _write_part(out, t, frows, trows, params_hash, csv_sha,
                                    skipped_no_1m_anchor=skipped)
        print(f"{t}: {per_ticker[t]['status']} feat={len(frows)} "
              f"sup={per_ticker[t]['supervised']} cens={per_ticker[t]['censored']} "
              f"skip1m={skipped}")

    missing = [t for t in tickers if per_ticker[t]["status"] in ("no_csv", "not_in_universe")]

    def _sum(key):
        return sum(v.get(key, 0) for v in per_ticker.values())

    manifest = {
        "dataset_version": DATASET_VERSION,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "label_cfg": {"take_atr": LABEL_CFG.take_atr, "stop_atr": LABEL_CFG.stop_atr,
                      "max_bars": LABEL_CFG.max_bars,
                      "atr_period": LABEL_CFG.atr_period, "tf": SUPERVISED_TF,
                      "session": {"tz": SESSION_TZ, "open": SESSION_OPEN,
                                  "close": SESSION_CLOSE}, "wall_clock": True},
        "label_cfg_hash": LABEL_CFG_HASH,
        "eligibility_policy": ELIGIBILITY,
        "eligibility_policy_hash": ELIGIBILITY_POLICY_HASH,
        "label_status_reason_values": ["gap", "session_cutoff", "anchor_incomplete",
                                       "future_bucket_incomplete"],
        "tf_seconds": list(TF_SECONDS),
        "quorum": QUORUM,
        "voice_fresh_min": VOICE_FRESH_MIN,
        "warmup_days": WARMUP_DAYS,
        "feature_columns": [{"name": n, "type": t, "role": _column_roles()[n]}
                            for n, t in _feature_columns()],
        "predictor_columns": _predictor_columns(),
        "metadata_columns": list(_METADATA_COLUMNS),
        "target_columns": [{"name": n, "type": t} for n, t in _TARGET_COLUMNS],
        "start": start.isoformat(), "end": end.isoformat(),
        "snapshot": str(args.snapshot),
        "input_manifest_sha256": _sha_of(args.snapshot / "manifest.json"),
        "code_sha256": code_sha,
        "modules": _module_shas(),
        "params_hash": params_hash,
        "n_tickers_total": len(tickers),
        "n_tickers_ok": sum(1 for v in per_ticker.values() if v["status"] == "ok"),
        "missing_tickers": missing,
        "feature_rows": _sum("feature_rows"), "target_rows": _sum("target_rows"),
        "supervised_rows": _sum("supervised"), "censored_rows": _sum("censored"),
        "parts": [{"ticker": t, "features": f"features/features_{t}.parquet",
                   "targets": f"targets/targets_{t}.parquet",
                   "meta": f"meta/meta_{t}.json",
                   "rows": per_ticker[t].get("feature_rows", 0)}
                  for t in tickers if per_ticker[t]["status"] == "ok"],
        "layout": {"features": "features/", "targets": "targets/", "meta": "meta/",
                   "note": "features = 1m decisions; targets = 5m closes; "
                           "join строго по row_id; supervised 1:1"},
        "label_status_values": ["MATURE", "CENSORED"],
        "per_ticker": per_ticker,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, default=str))
    feature_schema = {
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "dataset_version": DATASET_VERSION,
        "supervised_tf_seconds": SUPERVISED_TF,
        "tf_seconds": list(TF_SECONDS),
        "bias": {"tf_seconds": BIAS_TF, "period": BIAS_PERIOD},
        "volume_ratio_window": VOLR_WINDOW,
        "warmup_days": WARMUP_DAYS,
        "session": {"tz": SESSION_TZ, "open": SESSION_OPEN, "close": SESSION_CLOSE},
        "label": {"cfg_hash": LABEL_CFG_HASH, "cfg": manifest["label_cfg"]},
        "eligibility_policy": ELIGIBILITY,
        "eligibility_policy_hash": ELIGIBILITY_POLICY_HASH,
        "predictor_columns": _predictor_columns(),
        "metadata_columns": list(_METADATA_COLUMNS),
        "column_roles": _column_roles(),
        "voice_setups": [{"id": s, "tf_seconds": tf, "params": prm}
                         for s, tf, prm in VOICE_SETUPS],
        "voice_fresh_min": VOICE_FRESH_MIN,
        "quorum": QUORUM,
        "normalizations": ["rsi14/price", "macd_hist/price^0", "macd_hist_delta/price^0",
                           "ema50_dist=(close-ema50)/close", "ema50_slope=(ema_j-ema_j-1)/close",
                           "atr14_price=atr14/close", "volr20=vol/vol_sma20",
                           "n_min_<tf>=число 1m минут в корзине (coverage)"],
        "warmup_real": {"atr14": "первый валидный индекс 13",
                        "ema50": "первый валидный индекс 49",
                        "rsi14": "первый валидный индекс 14",
                        "macd12/26/9": "по каноническому warmup IndicatorHub",
                        "volr20": "первый валидный индекс 19"},
        "features": [{"name": n, "type": t, "role": _column_roles()[n]}
                     for n, t in _feature_columns()],
        "targets": [{"name": n, "type": t} for n, t in _TARGET_COLUMNS],
    }
    (out / "feature_schema.json").write_text(
        json.dumps(feature_schema, indent=1, default=str))
    print(f"feature_rows={manifest['feature_rows']} supervised={manifest['supervised_rows']} "
          f"censored={manifest['censored_rows']} ok={manifest['n_tickers_ok']}/{len(tickers)} out={out}")
    return manifest


if __name__ == "__main__":
    main()