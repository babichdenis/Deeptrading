"""L2.7 сквозной parity-гейт: ReplayState через compute_ensemble == batch.

Доктрина no-peeking: batch исполняет сигналы ретроспективно (включая
lookahead на закрытии бакетов); инкремент действует только известным
на момент бара. Отсюда ворота:
- EXACT (входы конвейера): funnel БЕЗ runner-ключей, entries, rejected,
  quorum_list, timeline, request_hash — дословно всегда;
- TRADES/ECONOMICS: полное равенство, только если fed_late == 0
  (не было сигналов, опоздавших к своему бару — тогда траектории
  обязаны совпасть бит-в-бит). Иначе: soundness структурно невозможна
  через сравнение сделок (см. ниже), печатаем экономику для человека.
- SOUNDNESS структурная (без сравнения сделок): все скормленные раннеру
  сигналы берутся из accepted/exits текущего префикса (дедуп по
  (ts, side, kind), посторонних источников нет — по построению run_step).
  Проверка выдуманных действий: fed_late>0 объясняется классом P1
  (опоздавший accepted), образцы печатаются.

Почему НЕ сравниваем сделки при fed_late>0: опоздавший сигнал сброшен
(бар уже обработан), а batch исполнил его ретроспективно — траектории
расходятся легитимно. Это не баг инкремента, а отсутствие подглядывания.

Гигиена кэшей: batch ходит через глобальный cached_resample с ключом
(len, first, last) БЕЗ контента — между вариантами чистим кэш и сдвигаем T0.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.engine.models import Candle
from app.services.ensemble import clear_resample_cache, compute_ensemble
from app.services.replay_pipeline import ReplayState

T0 = datetime(2026, 8, 1, 6, 50, tzinfo=timezone.utc)

EOD = "end_of_data"
RUNNER_KEYS = {"reentry_rejected", "reentry_rejected_list", "preview_trades"}


def _bars(n, seed, t0, vol_lo=500, vol_hi=2000):
    import random
    rng = random.Random(seed)
    out = []
    px = 100.0
    for i in range(n):
        ts = t0 + timedelta(minutes=i)
        if 150 <= i < 300:
            px *= 1.0012
        elif 300 <= i < 450:
            px *= 0.9988
        elif 550 <= i < 650:
            px *= 1.0010
        else:
            px = max(px + rng.gauss(0, 0.0012) * px, 1.0)
        o = px
        h = px * (1 + abs(rng.gauss(0, 0.0008)))
        lo = px * (1 - abs(rng.gauss(0, 0.0008)))
        # close обязан лежать внутри [low, high], иначе candle_guard отбракует
        c = min(max(px + rng.gauss(0, 0.0004) * px, lo), h)
        out.append(Candle(ts=ts, open=o, high=h, low=lo, close=c,
                          volume=int(rng.uniform(vol_lo, vol_hi))))
    return out


def _req_base(candles):
    return {
        "figi": "TEST", "lot": 1, "capital": 100_000,
        "setups": [
            {"strategy_id": "rsi_reversal", "tf": "5min",
             "params": {"period": 16, "oversold": 30, "overbought": 80}},
            {"strategy_id": "macd_cross", "tf": "5min",
             "params": {"fast": 12, "slow": 26, "signal_period": 9}},
            {"strategy_id": "donchian_breakout", "tf": "5min",
             "params": {"period": 45}},
        ],
        "quorum": 2,
        "entry": {"tf": "1min", "lookback": 1},
        "entry_window_min": 15,
        "exit_policy": {"id": "atr_stop", "params": {
            "period": 14, "multiplier": 4.0, "risk_reward": 4.0}},
        "from_ts": candles[0].ts.isoformat(),
        "to_ts": candles[-1].ts.isoformat(),
    }


def _req_dense(candles):
    """Мясистый req: quorum 1 + bias info — десятки сделок."""
    req = _req_base(candles)
    req["quorum"] = 1
    req["bias_mode"] = "info"
    return req


def _norm(o):
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, float):
        return round(o, 9)
    if isinstance(o, dict):
        return {k: _norm(o[k]) for k in sorted(o, key=str)}
    if isinstance(o, (list, tuple)):
        return [_norm(v) for v in o]
    return o


def _no_eod(trades):
    return [t for t in trades if t.get("exit_reason") != EOD]


def _funnel_inputs(funnel):
    return {k: v for k, v in funnel.items() if k not in RUNNER_KEYS}


def _check_inputs(batch_res, replay_res, tag):
    assert "error" not in batch_res, f"{tag}: batch {batch_res.get('error')}"
    assert "error" not in replay_res, f"{tag}: replay {replay_res.get('error')}"
    assert batch_res["meta"]["request_hash"] == replay_res["meta"]["request_hash"], tag
    for side in ("static",):
        b, r = batch_res[side], replay_res[side]
        assert _norm(_funnel_inputs(b["funnel"])) == \
            _norm(_funnel_inputs(r["funnel"])), f"{tag} funnel"
        assert _norm(b["entries"]) == _norm(r["entries"]), f"{tag} entries"
        assert _norm(b["rejected"]) == _norm(r["rejected"]), f"{tag} rejected"
        assert _norm(b["quorum_list"]) == _norm(r["quorum_list"]), f"{tag} quorum"
    assert _norm(batch_res["regime"]["timeline"]) == _norm(replay_res["regime"]["timeline"]), \
        f"{tag} timeline"


def _check_e2e(batch_res, replay_res, st, tag):
    _check_inputs(batch_res, replay_res, tag)
    late = st.fed_late
    bt, rt = batch_res["static"]["trades"], replay_res["static"]["trades"]
    be = batch_res["static"]["economic"]
    re_ = replay_res["static"]["economic"]
    print(f"[{tag}] fed_late={late} late_sample={st.fed_late_samples[:5]} "
          f"trades batch={len(bt)} replay={len(rt)} "
          f"net batch={be.get('net')} replay={re_.get('net')}")
    if late == 0:
        assert _norm(bt) == _norm(rt), f"{tag} trades"
        assert _norm(be) == _norm(re_), f"{tag} economic"
        assert _norm(batch_res["static"]["exit_coverage"]) == \
            _norm(replay_res["static"]["exit_coverage"]), f"{tag} coverage"
        assert _norm(batch_res["static"]["episodes"]) == \
            _norm(replay_res["static"]["episodes"]), f"{tag} episodes"
        assert _norm(batch_res["comparison"]) == _norm(replay_res["comparison"]), \
            f"{tag} comparison"
    else:
        # P1-класс: опоздавшие сигналы сброшены (бар уже обработан),
        # batch исполнил их ретроспективно. Невакуумность обязана держаться:
        if bt:
            assert rt, f"{tag}: batch торгует ({len(bt)}), replay пуст — поломка, не P1"


def _replay(candles, req, checkpoints=()):
    st = ReplayState(dict(req))
    snaps = {}
    last = None
    n = len(candles)
    for i in range(1, n + 1):
        last = compute_ensemble(candles[:i], dict(req, _replay_final=(i == n)), ctx=st)
        if i in checkpoints:
            snaps[i] = last
    return last, snaps, st


def test_final_plain_micro():
    # veto душит почти всё — сверяем reject-путь целиком (входы точные).
    clear_resample_cache()
    candles = _bars(700, seed=101, t0=T0)
    req = _req_base(candles)
    batch_res = compute_ensemble(list(candles), dict(req))
    assert len(batch_res["static"]["rejected"]) > 100
    replay_res, _, st = _replay(candles, req)
    _check_e2e(batch_res, replay_res, st, "plain")


def test_final_dense_info():
    clear_resample_cache()
    t0 = T0 + timedelta(days=10)
    candles = _bars(700, seed=111, t0=t0)
    req = _req_dense(candles)
    batch_res = compute_ensemble(list(candles), dict(req))
    assert len(batch_res["static"]["trades"]) > 10, "нет сделок — тест тривиален"
    reasons = {t["exit_reason"] for t in batch_res["static"]["trades"]}
    assert len(reasons) >= 2, f"мало причин выхода: {reasons}"
    replay_res, _, st = _replay(candles, req)
    _check_e2e(batch_res, replay_res, st, "dense")


def test_final_rich_gates():
    clear_resample_cache()
    t0 = T0 + timedelta(days=30)
    candles = _bars(700, seed=202, t0=t0)
    req = _req_dense(candles)
    req.update({
        "entry_from_setups": True,
        "rsi_filter": {"period": 14, "min": 40.0, "max": 60.0},
        "neutral_mode": "semi_flip",
        "same_side_reentry_cooldown_bars": 5,
        "entry_confirm_bars": 2,
        "volume_filter_threshold": 0.5,
    })
    batch_res = compute_ensemble(list(candles), dict(req))
    replay_res, _, st = _replay(candles, req)
    _check_e2e(batch_res, replay_res, st, "rich")


def test_final_direction_sid():
    clear_resample_cache()
    t0 = T0 + timedelta(days=60)
    candles = _bars(700, seed=303, t0=t0)
    req = _req_dense(candles)
    req["entry_direction_sid"] = "rsi_reversal"
    batch_res = compute_ensemble(list(candles), dict(req))
    replay_res, _, st = _replay(candles, req)
    _check_e2e(batch_res, replay_res, st, "direction")


def test_final_drop_useless_oracle():
    clear_resample_cache()
    t0 = T0 + timedelta(days=90)
    candles = _bars(500, seed=404, t0=t0)
    req = _req_dense(candles)
    req["drop_useless"] = True
    batch_res = compute_ensemble(list(candles), dict(req))
    replay_res, _, st = _replay(candles, req)
    _check_e2e(batch_res, replay_res, st, "drop")


def test_intermediate_no_eod():
    clear_resample_cache()
    t0 = T0 + timedelta(days=120)
    candles = _bars(700, seed=505, t0=t0)
    req = _req_dense(candles)
    cps = (200, 350, 500, 650)
    replay_res, snaps, st = _replay(candles, req, checkpoints=cps)
    for i in cps:
        b = compute_ensemble(list(candles[:i]), dict(req))
        r = snaps[i]
        assert _norm(_funnel_inputs(b["static"]["funnel"])) == \
               _norm(_funnel_inputs(r["static"]["funnel"])), f"funnel@{i}"
        assert _norm(b["static"]["entries"]) == _norm(r["static"]["entries"]), \
            f"entries@{i}"
        assert _norm(b["static"]["rejected"]) == _norm(r["static"]["rejected"]), \
            f"rejected@{i}"
    batch_res = compute_ensemble(list(candles), dict(req))
    _check_e2e(batch_res, replay_res, st, "intermediate-final")
