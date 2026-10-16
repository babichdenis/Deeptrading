"""ML-MTF-1 / B1 — focused-тесты датасета v3 (REWORK архитектора 2026-10-14).

Проверяют ровно то, что вскрыли architect proof/приёмка, и что критично для обучения:
- ЕДИНАЯ eligibility-policy: будни + главная сессия 10:00–18:40 MSK применяется
  ОДНИМ predicate и к features, и к меткам (выходные не попадают в датасет);
- выравнивание метки: decision_ts использует канонический `index` triple_barrier
  (первый валидный бар ATR — индекс 13), а не смещённый enumerate-порядок;
- label_end_ts = ЗАКРЫТИЕ t1-бара; сессионная разметка: горизонт только внутри
  главной сессии, wall-clock 24 слота, пропуски не сжимаются;
- КАЖДАЯ будущая 5m-корзина проверяется на полноту ДО low/high: неполная
  будущая свеча ⇒ CENSORED(future_bucket_incomplete), а не MATURE;
- CENSORED (не 0) с reason: session_cutoff / gap / anchor_incomplete /
  future_bucket_incomplete;
- coverage: n_min_<tf> (частичная корзина → ячейки None, факт сохранён);
- канонические индикаторы IndicatorHub с реальным warmup (ATR 13/EMA 49/RSI 14);
- голоса: активны только в lookback FRESH_MIN=30, age отдельно; ошибка источника
  → voice_status="error:<Class>" и None (missing), не фиктивный 0;
- features на каждом eligible 1m, supervised targets только на закрытиях 5m,
  join строго по row_id (1:1 по supervised);
- роли колонок: predictor_columns (рынок+голоса) vs metadata_columns (n_min/
  voice_status/identity); policy hash в manifest;
- immutable run + resume с учётом input-hash (изменение CSV → пересчёт).
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "scripts"))

import pytest

from app.engine.models import Candle as EngineCandle
import ml_mtf_dataset as m

UTC = timezone.utc
T0 = datetime(2026, 3, 2, 7, 0, tzinfo=UTC)   # понедельник, 10:00 MSK


def m1_series(n=480, start=T0, px0=100.0):
    """Синтетические 1m бары: непрерывные, с детерминированным «дрожанием»."""
    out = []
    for i in range(n):
        ts = start + timedelta(minutes=i)
        close = px0 + (i % 7) * 0.5 - (i % 3) * 0.3
        out.append(EngineCandle(ts=ts, open=close - 0.05, high=close + 0.2,
                                low=close - 0.2, close=close, volume=100.0 + i % 5))
    return out


def m1_5min(n5=60, px=100.0, spike_at=None, hole_at=None, hole_keep_minutes=None):
    """1m бары, дающие n5 5m-баров. flat серия: high=+1/low=−1 → ATR=2,
    барьеры ±3. spike_at → бар с размахом ±4 (задевает оба барьера → stop-first).
    hole_at → корзина отсутствует целиком ИЛИ (hole_keep_minutes) частично."""
    minutes = []
    for j in range(n5):
        base = T0 + timedelta(minutes=5 * j)
        if hole_at == j:
            if hole_keep_minutes is None:
                continue
            keep = set(hole_keep_minutes)
        else:
            keep = set(range(5))
        for k in range(5):
            if k not in keep:
                continue
            ts = base + timedelta(minutes=k)
            hi, lo = px + 1.0, px - 1.0
            if spike_at == j:
                hi, lo = px + 4.0, px - 4.0
            minutes.append(EngineCandle(ts=ts, open=px, high=hi, low=lo,
                                        close=px, volume=10.0))
    return minutes


def feature_map(m1):
    """decision_ts(iso) -> строка признаков (ровно то, что отдаёт compute_ticker)."""
    bars = m._resample_all(m1)
    vidx = m._voice_index(bars)
    rows = m._continuous_features_1m(m1, bars, vidx,
                                     m1[0].ts, m1[-1].ts + timedelta(minutes=1))
    return {dt.isoformat(): row for dt, row in rows}


def _scale(c, k):
    return EngineCandle(ts=c.ts, open=c.open * k, high=c.high * k,
                        low=c.low * k, close=c.close * k, volume=c.volume)


def _write_csv(dirp, bars, ticker="T", figi="F"):
    dirp = Path(dirp)
    dirp.mkdir(parents=True, exist_ok=True)
    p = dirp / f"{ticker}__m1.csv"
    with p.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ticker", "figi", "interval", "ts", "open", "high", "low",
                    "close", "volume", "source", "partial"])
        for c in bars:
            w.writerow([ticker, figi, 1, c.ts.isoformat(), c.open, c.high,
                        c.low, c.close, c.volume, "db", ""])
    return p


def _tmp():
    import tempfile
    return Path(tempfile.mkdtemp())


# ------------------------------------------------ REWORK п.1: выравнивание метки
def test_label_alignment_uses_index():
    csvp = _write_csv(_tmp(), m1_series(480), figi="F")
    _, tr, _ = m.compute_ticker(csvp, "F", "T", T0, T0 + timedelta(hours=8))
    bars5 = m._resample_all(m1_series(480))[m.SUPERVISED_TF]
    atr = m._atr(bars5, m.ATR_P)
    first_idx = next(i for i, a in enumerate(atr) if a is not None and a > 0)
    assert first_idx == 13
    expect0 = (bars5[first_idx].ts + timedelta(seconds=300)).isoformat()
    assert tr[0]["decision_ts"] == expect0
    # proof architect: каноническое решение = 08:10 (старая версия отдавала 07:05)
    assert tr[0]["decision_ts"] == "2026-03-02T08:10:00+00:00"
    # все decision лежат на закрытиях 5m (никакого enumerate-сдвига)
    for r in tr:
        assert (datetime.fromisoformat(r["decision_ts"]) - T0).total_seconds() % 300 == 0


def test_atr_invalid_stretch_does_not_shift():
    # первая сотня минут без размаха (ATR=0 → канонический skip), затем волатильность
    flat = [EngineCandle(ts=T0 + timedelta(minutes=i), open=100.0, high=100.0,
                         low=100.0, close=100.0, volume=1.0) for i in range(120)]
    m1 = flat + m1_series(240, start=T0 + timedelta(minutes=120))
    csvp = _write_csv(_tmp(), m1, figi="F")
    _, tr, _ = m.compute_ticker(csvp, "F", "T", T0, T0 + timedelta(hours=8))
    bars5 = m._resample_all(m1)[m.SUPERVISED_TF]
    atr = m._atr(bars5, m.ATR_P)
    first_ok = next(i for i, a in enumerate(atr) if a is not None and a > 0)
    assert first_ok > 13                       # плоский хвост — ATR ещё невалиден
    expect0 = (bars5[first_ok].ts + timedelta(seconds=300)).isoformat()
    assert tr[0]["decision_ts"] == expect0     # привязка к бар-индексу, не к enumerate


# -------------------------------------------- REWORK п.2: label_end_ts = t1 close
def test_label_end_is_t1_close():
    m1 = m1_series(480)
    bars5 = m._resample_all(m1)[m.SUPERVISED_TF]
    tr = m._session_label_rows(bars5, m1, T0, T0 + timedelta(hours=8))
    row = next(r for r in tr if r["index"] == 13)
    # полный горизонт без касаний → label 0, исход известен на закрытии t1 = i+24
    assert row["label"] == 0 and row["label_status"] == "MATURE"
    t1 = 13 + m.LABEL_CFG.max_bars
    assert row["label_end_ts"] == (bars5[t1].ts + timedelta(seconds=300)).isoformat()
    assert row["label_end_ts"] == (
        datetime.fromisoformat(row["decision_ts"]) + timedelta(minutes=120)).isoformat()


def test_touch_label_end_is_touch_bar_close():
    m1b = m1_5min(60, spike_at=20)
    bars5b = m._resample_all(m1b)[m.SUPERVISED_TF]
    trb = m._session_label_rows(bars5b, m1b, T0, T0 + timedelta(minutes=5 * 60))
    rowb = next(r for r in trb if r["index"] == 14)
    assert rowb["label_status"] == "MATURE" and rowb["label"] == -1   # double-touch → stop-first
    assert rowb["t1_index"] == 20
    assert rowb["label_end_ts"] == (bars5b[20].ts + timedelta(seconds=300)).isoformat()


# --------------------------------------------- REWORK п.2: сессия/gap/CENSORED
def test_overnight_target_not_mature():
    # понедельник 10:00–18:40 MSK полностью, вторник короткий; ночь — граница сессии
    day1 = [EngineCandle(ts=T0 + timedelta(minutes=i), open=100.0, high=101.0,
                         low=99.0, close=100.0, volume=10.0) for i in range(520)]
    tue = datetime(2026, 3, 3, 7, 0, tzinfo=UTC)
    day2 = [EngineCandle(ts=tue + timedelta(minutes=i), open=100.0, high=101.0,
                         low=99.0, close=100.0, volume=10.0) for i in range(120)]
    m1 = day1 + day2
    bars5 = m._resample_all(m1)[m.SUPERVISED_TF]
    tr = m._session_label_rows(bars5, m1, T0, T0 + timedelta(days=1, hours=2))
    # нет MATURE-исхода, известного за пределами сессии (proof: 08:05→08:25 → CENSORED)
    for r in tr:
        if r["label_status"] == "MATURE" and r["label_end_ts"]:
            assert datetime.fromisoformat(r["label_end_ts"]).date() == datetime(2026, 3, 2).date()
    late = next(r for r in tr if r["index"] >= 88)     # решение ~17:25 MSK
    assert late["label_status"] == "CENSORED"
    assert late["reason"] == "session_cutoff"


def test_gap_censors_unless_touch_before():
    # дыра в корзине 25: решение j=14, горизонт проходит через дыру → CENSORED(gap)
    m1 = m1_5min(60, hole_at=25)
    bars5 = m._resample_all(m1)[m.SUPERVISED_TF]
    tr = m._session_label_rows(bars5, m1, T0, T0 + timedelta(minutes=5 * 60))
    row = next(r for r in tr if r["index"] == 14)
    assert row["label_status"] == "CENSORED" and row["reason"] == "gap"
    assert row["label"] is None and row["label_end_ts"] is None
    # касание ДО дыры → исход известен, MATURE (дыра после касания не мешает)
    m1b = m1_5min(60, spike_at=20, hole_at=25)
    bars5b = m._resample_all(m1b)[m.SUPERVISED_TF]
    trb = m._session_label_rows(bars5b, m1b, T0, T0 + timedelta(minutes=5 * 60))
    rowb = next(r for r in trb if r["index"] == 14)
    assert rowb["label_status"] == "MATURE" and rowb["label"] == -1
    assert rowb["t1_index"] == 20


def test_tail_is_censored_not_zero():
    m1 = m1_series(480)
    bars5 = m._resample_all(m1)[m.SUPERVISED_TF]
    tr = m._session_label_rows(bars5, m1, T0, T0 + timedelta(hours=8))
    statuses = {r["label_status"] for r in tr}
    assert statuses == {"MATURE", "CENSORED"}
    for r in tr:
        if r["label_status"] == "CENSORED":
            assert r["label"] is None             # CENSORED != метка 0
            assert r["reason"] in ("session_cutoff", "gap", "anchor_incomplete",
                                   "future_bucket_incomplete")


# --------------------------------------------- REWORK п.3: coverage / warmup
def test_partial_bucket_counts_and_anchor_censored():
    # корзина j=25 существует, но только 1 из 5 минут
    m1 = m1_5min(40, hole_at=25, hole_keep_minutes={2})
    bars5 = m._resample_all(m1)[m.SUPERVISED_TF]
    ind = m._ind_series(bars5, m.SUPERVISED_TF, m1)
    j25 = next(j for j, b in enumerate(bars5)
               if (b.ts - T0).total_seconds() // m.SUPERVISED_TF == 25)
    cells = m._cells_at(ind, j25, m.SUPERVISED_TF)
    assert cells["n_min"] == 1                    # факт неполноты сохранён
    assert cells["cells"]["rsi14_5m"] is None and cells["cells"]["atr14_price_5m"] is None
    # решение с неполной корзиной-якорем → CENSORED(anchor_incomplete), выход в ряд
    tr = m._session_label_rows(bars5, m1, T0, T0 + timedelta(minutes=5 * 40))
    row25 = next(r for r in tr if r["index"] == j25)
    assert row25["label_status"] == "CENSORED" and row25["reason"] == "anchor_incomplete"
    # полная корзина даёт значения
    cells1 = m._cells_at(ind, j25 - 1, m.SUPERVISED_TF)
    assert cells1["n_min"] == 5 and cells1["cells"]["rsi14_5m"] is not None


def test_sparse_anchor_skipped_when_no_1m_close_minute(tmp_path):
    """Якорная корзина частична и закрывающая 1m минута отсутствует: решения на
    этой минуте нет → target-строка не эмитируется, факт в skipped_no_1m_anchor."""
    m1 = m1_5min(40, hole_at=25, hole_keep_minutes={0, 1, 2})
    snap = tmp_path / "s"
    _write_csv(snap / "csv", m1, ticker="AAA", figi="F")
    out = tmp_path / "o"
    end = T0 + timedelta(minutes=5 * 40)
    man = m.main(["--snapshot", str(snap), "--start", T0.isoformat(),
                  "--end", end.isoformat(), "--out", str(out), "--tickers", "AAA"])
    meta = json.loads((out / "meta" / "meta_AAA.json").read_text())
    assert meta["skipped_no_1m_anchor"] >= 1
    import pyarrow.parquet as pq
    tu = pq.read_table(out / "targets" / "targets_AAA.parquet").to_pydict()
    bad = T0 + timedelta(minutes=5 * 26)           # закрытие отсутствующей минуты
    assert bad.isoformat() not in tu["decision_ts"]
    assert man["target_rows"] == len(tu["decision_ts"])
    assert man["supervised_rows"] >= 0 and man["censored_rows"] == man["target_rows"] - \
        man["supervised_rows"]


def test_canonical_indicator_warmup():
    m1 = m1_series(1200)
    bars5 = m._resample_all(m1)[m.SUPERVISED_TF]
    ind = m._ind_series(bars5, m.SUPERVISED_TF, m1)
    assert ind["atr"][:13] == [None] * 13 and ind["atr"][13] is not None
    assert ind["atr"][13] == m._atr(bars5, m.ATR_P)[13]   # ровно та же canonical-серия
    assert ind["ema"][:49] == [None] * 49 and ind["ema"][49] is not None   # EMA50 warmup
    assert ind["rsi"][:14] == [None] * 14 and ind["rsi"][14] is not None
    # в exported-строках: ema50_dist_5m None до бара 49, значение с бара 49
    fm = feature_map(m1)
    d48 = (bars5[48].ts + timedelta(seconds=300)).isoformat()
    d49 = (bars5[49].ts + timedelta(seconds=300)).isoformat()
    assert fm[d48]["ema50_dist_5m"] is None
    assert fm[d49]["ema50_dist_5m"] is not None


# ------------------------------------------------- REWORK п.4: голоса
def test_stale_voice_not_active_and_error_status(monkeypatch):
    m1 = m1_series(480)
    bars = m._resample_all(m1)
    # голос 5 минут от начала, решение в конце окна: возраст ~475 мин → не активен
    old_vidx = {sid: ([T0 + timedelta(minutes=5)], [1], "ok")
                for sid, _, _ in m.VOICE_SETUPS}
    rows = m._continuous_features_1m(m1, bars, old_vidx, T0, T0 + timedelta(hours=8))
    last = max(rows, key=lambda r: r[0])[1]
    assert last["vote_rsi_reversal"] == 0
    assert last["age_rsi_reversal"] is not None and last["age_rsi_reversal"] > 300
    assert last["voice_buy"] == 0
    # свежий голос (возраст 3 мин ≤ FRESH_MIN=30) → активен
    fresh_vidx = {sid: ([T0 + timedelta(minutes=475)], [1], "ok")
                  for sid, _, _ in m.VOICE_SETUPS}
    rows2 = m._continuous_features_1m(m1, bars, fresh_vidx, T0, T0 + timedelta(hours=8))
    row2 = next(r for r in rows2 if r[0] == T0 + timedelta(minutes=478))[1]
    assert row2["vote_rsi_reversal"] == 1 and row2["voice_buy"] == 9

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(m, "generate_signals", boom)
    vidx3 = m._voice_index(bars)
    assert vidx3[m.VOICE_SETUPS[0][0]][2] == "error:RuntimeError"
    rows3 = m._continuous_features_1m(m1, bars, vidx3, T0, T0 + timedelta(hours=8))
    r3 = rows3[10][1]
    assert r3["voice_status_rsi_reversal"] == "error:RuntimeError"
    assert r3["vote_rsi_reversal"] is None      # missing, не фиктивный 0
    assert r3["age_rsi_reversal"] is None
    assert r3["voice_buy"] == 0 and r3["voice_conflict"] == 0


# ----------------------------------------------------- closed-only / bias
def test_tf_features_use_only_closed_bars():
    before = m1_series(1200)
    after = [(_scale(c, 5.0) if 480 <= i < 510 else c) for i, c in enumerate(before)]
    fb, fa = feature_map(before), feature_map(after)
    t1 = (T0 + timedelta(minutes=475)).isoformat()   # 30m-корзина [15:00,15:30) не закрыта
    t2 = (T0 + timedelta(minutes=510)).isoformat()   # закрыта
    assert fb[t1]["atr14_price_1800s"] is not None
    assert fb[t1]["atr14_price_1800s"] == fa[t1]["atr14_price_1800s"]
    assert fb[t2]["atr14_price_1800s"] != fa[t2]["atr14_price_1800s"]


def test_future_bars_do_not_change_past_features():
    before = m1_series(480)
    after = [(_scale(c, 4.0) if i >= 120 else c) for i, c in enumerate(before)]  # >= 09:00
    fb, fa = feature_map(before), feature_map(after)
    t_cut = (T0 + timedelta(minutes=120)).isoformat()
    for ts, row in fb.items():
        if ts <= t_cut:
            assert row == fa[ts], f"признаки при {ts} изменились от будущих баров"


def test_10m_and_30m_features_present_and_finite():
    fm = feature_map(m1_series(2400))
    d = (T0 + timedelta(minutes=1500)).isoformat()   # вт 08:00 UTC = 11:00 MSK
    row = fm[d]
    for suf in ("600s", "1800s"):
        assert row[f"rsi14_{suf}"] is not None
        assert row[f"atr14_price_{suf}"] is not None
        assert row[f"macd_hist_{suf}"] is not None


def test_bias_present_and_closed_only():
    before = m1_series(1200)
    after = [(_scale(c, 5.0) if 480 <= i < 540 else c) for i, c in enumerate(before)]
    fb, fa = feature_map(before), feature_map(after)
    t = (T0 + timedelta(minutes=475)).isoformat()   # час 15:00 ещё не закрыт
    assert fb[t]["bias"] in (-1, 1)
    assert fb[t]["bias"] == fa[t]["bias"]


# ------------------------------------- REWORK п.6: 1m features + 5m targets, join
def test_1m_features_5m_targets_join_1to1(tmp_path):
    import pyarrow.parquet as pq
    snap = tmp_path / "snap"
    _write_csv(snap / "csv", m1_series(480), ticker="AAA", figi="FIGI_A")
    out = tmp_path / "out"
    end = T0 + timedelta(hours=8)
    man = m.main(["--snapshot", str(snap), "--start", T0.isoformat(),
                  "--end", end.isoformat(), "--out", str(out), "--tickers", "AAA"])
    fu = pq.read_table(out / "features" / "features_AAA.parquet").to_pydict()
    tu = pq.read_table(out / "targets" / "targets_AAA.parquet").to_pydict()
    assert "label" not in fu                       # признаки без меток (метки отдельно)
    assert len(fu["row_id"]) == man["feature_rows"] > 0
    assert len(set(fu["row_id"])) == len(fu["row_id"])
    assert len(set(tu["row_id"])) == len(tu["row_id"])
    fid, tid = set(fu["row_id"]), set(tu["row_id"])
    assert tid <= fid                              # targets ⊆ features (join по row_id)
    assert len(tid) == man["target_rows"]
    cnt = {}
    for rid in fu["row_id"]:
        cnt[rid] = cnt.get(rid, 0) + 1
    assert all(cnt[rid] == 1 for rid in tid)       # 1:1 по supervised
    for dt in tu["decision_ts"]:                   # targets — только закрытия 5m
        assert (datetime.fromisoformat(dt) - T0).total_seconds() % 300 == 0
    for st, lab in zip(tu["label_status"], tu["label"]):
        if st == "MATURE":
            assert lab in (-1, 0, 1)
        else:
            assert lab is None
    assert man["supervised_rows"] == sum(1 for s in tu["label_status"] if s == "MATURE")
    assert man["censored_rows"] == sum(1 for s in tu["label_status"] if s == "CENSORED")
    assert man["supervised_rows"] + man["censored_rows"] == man["target_rows"]
    ft = pq.read_table(out / "features" / "features_AAA.parquet")
    assert ft.column_names == [c for c, _ in m._feature_columns()]
    assert pq.read_table(out / "targets" / "targets_AAA.parquet").column_names == \
        [c for c, _ in m._TARGET_COLUMNS]
    # параметры/конфиг зафиксированы в manifest и feature_schema.json
    assert man["params_hash"] and man["label_cfg_hash"] == m.LABEL_CFG_HASH
    assert (out / "feature_schema.json").exists()


def test_repeated_run_fingerprint_stable(tmp_path):
    import pyarrow.parquet as pq
    snap = tmp_path / "s"
    _write_csv(snap / "csv", m1_series(480), ticker="AAA", figi="F")
    args = lambda o: ["--snapshot", str(snap), "--start", T0.isoformat(),
                      "--end", (T0 + timedelta(hours=8)).isoformat(),
                      "--out", str(o), "--tickers", "AAA"]
    o1, o2 = tmp_path / "o1", tmp_path / "o2"
    man1, man2 = m.main(args(o1)), m.main(args(o2))
    a = pq.read_table(o1 / "features" / "features_AAA.parquet").to_pydict()
    b = pq.read_table(o2 / "features" / "features_AAA.parquet").to_pydict()
    assert a["row_id"] == b["row_id"]
    assert a["rsi14_5m"] == b["rsi14_5m"]
    ta = pq.read_table(o1 / "targets" / "targets_AAA.parquet").to_pydict()
    tb = pq.read_table(o2 / "targets" / "targets_AAA.parquet").to_pydict()
    assert ta["row_id"] == tb["row_id"] and ta["label"] == tb["label"]
    assert man1["params_hash"] == man2["params_hash"]
    assert ("input_manifest_sha256" in man1 and "modules" in man1)


def test_immutable_run_refuses_different_params(tmp_path):
    snap = tmp_path / "s"
    _write_csv(snap / "csv", m1_series(480), ticker="AAA", figi="F")
    out = tmp_path / "o"
    base = ["--snapshot", str(snap), "--start", T0.isoformat(),
            "--out", str(out), "--tickers", "AAA"]
    m.main(base + ["--end", (T0 + timedelta(hours=8)).isoformat()])
    m.main(base + ["--end", (T0 + timedelta(hours=8)).isoformat()])   # тот же набор — ок
    with pytest.raises(SystemExit):                                   # другой набор — отказ
        m.main(base + ["--end", (T0 + timedelta(hours=9)).isoformat()])
    man = m.main(base + ["--end", (T0 + timedelta(hours=9)).isoformat(), "--force"])
    assert man["n_tickers_ok"] == 1


def test_resume_recomputes_when_input_changes(tmp_path):
    snap = tmp_path / "s"
    csvp = _write_csv(snap / "csv", m1_series(480), ticker="AAA", figi="F")
    out = tmp_path / "o"
    args = ["--snapshot", str(snap), "--start", T0.isoformat(),
            "--end", (T0 + timedelta(hours=8)).isoformat(),
            "--out", str(out), "--tickers", "AAA"]
    m.main(args)
    f = out / "features" / "features_AAA.parquet"
    before = f.stat().st_mtime
    m.main(args + ["--resume"])                    # вход неизменён → skip части
    assert f.stat().st_mtime == before
    rows2 = m1_series(480)
    c = rows2[100]
    rows2[100] = EngineCandle(ts=c.ts, open=c.open, high=c.high, low=c.low,
                              close=c.close + 1.0, volume=c.volume)
    _write_csv(snap / "csv", rows2, ticker="AAA", figi="F")
    m.main(args + ["--resume"])                    # CSV изменился → csv_sha другой → пересчёт
    assert f.stat().st_mtime > before


def test_manifest_marks_missing_ticker(tmp_path):
    snap = tmp_path / "snap"
    _write_csv(snap / "csv", m1_series(120), ticker="AAA", figi="A")
    man = m.main(["--snapshot", str(snap), "--start", T0.isoformat(),
                  "--end", (T0 + timedelta(hours=2)).isoformat(),
                  "--out", str(tmp_path / "o"), "--tickers", "AAA", "ZZZ"])
    assert "ZZZ" in man["missing_tickers"]
    assert man["n_tickers_ok"] == 1


# ============================================ REWORK v3 п.1: единая eligibility
def test_weekday_eligibility_excludes_weekend():
    """Один predicate: features и метки не содержат решений в выходные (policy)."""
    sat = datetime(2026, 2, 28, 7, 0, tzinfo=UTC)     # суббота 10:00 MSK
    m1 = m1_series(3 * 24 * 60, start=sat)            # сб..вт (входные выходные есть)
    assert sum(1 for c in m1 if c.ts.astimezone(m.MSK).weekday() >= 5) > 0
    bars = m._resample_all(m1)
    vidx = m._voice_index(bars)
    end = sat + timedelta(days=3)
    frows = m._continuous_features_1m(m1, bars, vidx, sat, end)
    assert frows
    assert all(dt.astimezone(m.MSK).weekday() < 5 for dt, _ in frows)
    assert any(dt.astimezone(m.MSK).weekday() == 0 for dt, _ in frows)   # пн есть
    tr = m._session_label_rows(bars[m.SUPERVISED_TF], m1, sat, end)
    assert tr
    for r in tr:
        assert datetime.fromisoformat(r["decision_ts"]).astimezone(m.MSK).weekday() < 5
    assert all(m._in_session(c.ts) is False for c in m1
               if c.ts.astimezone(m.MSK).weekday() >= 5)


def test_predicate_identical_for_features_and_labels():
    """Проверка «один predicate»: eligible 5m-барам соответствуют решения features."""
    m1 = m1_series(600)
    bars = m._resample_all(m1)
    vidx = m._voice_index(bars)
    end = T0 + timedelta(hours=8, minutes=40)
    f_ts = {dt for dt, _ in m._continuous_features_1m(m1, bars, vidx, T0, end)}
    for r in m._session_label_rows(bars[m.SUPERVISED_TF], m1, T0, end):
        assert datetime.fromisoformat(r["decision_ts"]) in f_ts


# ============================ REWORK v3 п.2: completeness будущих корзин
def test_incomplete_future_bucket_censors_not_mature():
    """Неполная будущая 5m-корзина ⇒ CENSORED(future_bucket_incomplete), не MATURE."""
    for hole_at, keep in ((25, {1}), (30, {0, 1, 3, 4})):        # 1 мин и пропуск в середине
        m1 = m1_5min(60, hole_at=hole_at, hole_keep_minutes=keep)
        bars5 = m._resample_all(m1)[m.SUPERVISED_TF]
        tr = m._session_label_rows(bars5, m1, T0, T0 + timedelta(minutes=5 * 60))
        row = next(r for r in tr if r["index"] == 13)
        assert row["label_status"] == "CENSORED"
        assert row["reason"] == "future_bucket_incomplete"
        assert row["label"] is None and row["label_end_ts"] is None


def test_partial_future_bucket_with_touch_still_censored():
    """Даже если неполная свеча задевает барьер, первое касание недоказуемо."""
    m1 = m1_5min(60, spike_at=25, hole_at=25, hole_keep_minutes={1})
    bars5 = m._resample_all(m1)[m.SUPERVISED_TF]
    tr = m._session_label_rows(bars5, m1, T0, T0 + timedelta(minutes=5 * 60))
    row = next(r for r in tr if r["index"] == 13)
    assert row["label_status"] == "CENSORED"
    assert row["reason"] == "future_bucket_incomplete"


def test_early_hit_before_first_incomplete_bucket_is_mature():
    """Ранний подтверждённый hit в ПОЛНОЙ корзине до неполной остаётся MATURE."""
    m1 = m1_5min(60, spike_at=20, hole_at=25, hole_keep_minutes={1})
    bars5 = m._resample_all(m1)[m.SUPERVISED_TF]
    tr = m._session_label_rows(bars5, m1, T0, T0 + timedelta(minutes=5 * 60))
    row = next(r for r in tr if r["index"] == 13)
    assert row["label_status"] == "MATURE" and row["label"] == -1
    assert row["t1_index"] == 20


def test_timeout_requires_all_24_buckets_complete():
    """Timeout (label 0) допустим только после 24 ПОЛНЫХ корзин."""
    m1 = m1_5min(60, hole_at=36, hole_keep_minutes={2})    # внутри горизонта i+1..i+24
    bars5 = m._resample_all(m1)[m.SUPERVISED_TF]
    tr = m._session_label_rows(bars5, m1, T0, T0 + timedelta(minutes=5 * 60))
    row = next(r for r in tr if r["index"] == 13)
    assert row["reason"] == "future_bucket_incomplete"
    # без неполноты тот же якорь даёт timeout-метрку 0 (полные 24 слота)
    good = m1_5min(60)
    g = next(r for r in m._session_label_rows(
        m._resample_all(good)[m.SUPERVISED_TF], good, T0, T0 + timedelta(minutes=5 * 60))
        if r["index"] == 13)
    assert g["label"] == 0 and g["label_status"] == "MATURE"


# ==================== REWORK v3 п.3: роли колонок / policy в manifest
def test_predictor_and_metadata_roles_in_manifest(tmp_path):
    snap = tmp_path / "s"
    _write_csv(snap / "csv", m1_series(480), ticker="AAA", figi="F")
    out = tmp_path / "o"
    man = m.main(["--snapshot", str(snap), "--start", T0.isoformat(),
                  "--end", (T0 + timedelta(hours=8)).isoformat(),
                  "--out", str(out), "--tickers", "AAA"])
    preds, metas = set(man["predictor_columns"]), set(man["metadata_columns"])
    names = {c["name"] for c in man["feature_columns"]}
    assert preds and metas and preds.isdisjoint(metas)
    assert names == preds | metas
    assert "label" not in names
    assert not any(n.startswith("n_min_") or n.startswith("voice_status_") for n in preds)
    assert all(f"n_min_{s}" in metas for s in ("1m", "5m", "600s", "1800s", "3600s"))
    assert len(preds) == 35 + 1 + 18 + 4
    assert man["eligibility_policy"]["weekdays"] == [0, 1, 2, 3, 4]
    assert man["eligibility_policy_hash"] == m.ELIGIBILITY_POLICY_HASH
    assert "future_bucket_incomplete" in man["label_status_reason_values"]
    roles = json.loads((out / "feature_schema.json").read_text())["column_roles"]
    assert roles["n_min_5m"] == "metadata" and roles["voice_status_rsi_reversal"] == "metadata"
    assert roles["rsi14_5m"] == "predictor" and roles["bias"] == "predictor"
    assert man["dataset_version"] == "ml_mtf_1_v3"
    assert man["feature_schema_version"] == "1m_mtf_v3"


def test_validate_flags_no_weekend_and_allowed_reasons(tmp_path):
    snap = tmp_path / "s"
    _write_csv(snap / "csv", m1_series(480), ticker="AAA", figi="F")
    out = tmp_path / "o"
    m.main(["--snapshot", str(snap), "--start", T0.isoformat(),
            "--end", (T0 + timedelta(hours=8)).isoformat(),
            "--out", str(out), "--tickers", "AAA"])
    diag = m.validate_run(out)
    assert diag["ok"] is True and diag["errors"] == []
    assert diag["weekday_violations"] == 0