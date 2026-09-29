"""L2.6 parity-гейт раннера: IncrementalRunner == EngineRunner.run.

Матрица конфигов × сценариев × паттернов подачи. Сравнение: fingerprint,
все поля сделок, audit-лента, exit_coverage.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.engine import EngineConfig, EngineRunner, FixedSlTpPolicy, SignalPolicyConfig
from app.engine.exits import AtrStopPolicy
from app.engine.models import Candle
from app.engine.replay_runner import IncrementalRunner
from app.engine.sessions import SessionPolicyConfig
from app.engine.wave1 import ReplayStrategy

T0 = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)


def mkcandles(n, seed=5, step_min=5, base=100.0):
    import random
    rng = random.Random(seed)
    out = []
    px = base
    for i in range(n):
        ts = T0 + timedelta(minutes=step_min * i)
        if 40 <= i < 90:
            px *= 1.002
        elif 130 <= i < 180:
            px *= 0.998
        else:
            px = max(px + rng.gauss(0, 0.001) * px, 1.0)
        o = px
        c = px + rng.gauss(0, 0.0005) * px
        # ENG-015: генератор обязан давать валидные OHLC (high >= max(o,c) >= min >= low)
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.001)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, 0.001)))
        out.append(Candle(ts=ts, open=o, high=h, low=lo, close=c, volume=1000))
    return out


def sigs_at(candles, idx_side):
    return [(candles[i].ts, side) for i, side in idx_side]


def regime_rows(candles, states):
    return [{"ts": candles[i].ts, "state": s} for i, s in states]


def _norm_ledger(ledger):
    trades = [tuple(sorted(vars(t).items())) for t in ledger.trades]
    audit = [(a.index, a.time.isoformat(), a.kind, a.detail) for a in ledger.audit]
    return trades, audit


def check(candles, entries, exits, exit_policy, cfg, patterns):
    ref_runner = EngineRunner(strategy=ReplayStrategy(entries, exits=exits),
                              exit_policy=exit_policy, config=cfg)
    ref = ref_runner.run(list(candles))
    ref_t, ref_a = _norm_ledger(ref)
    ref_fp = ref.fingerprint()
    ref_cov = dict(ref_runner.exit_coverage)
    for name, chunks in patterns.items():
        r = IncrementalRunner(strategy=ReplayStrategy(entries, exits=exits),
                              exit_policy=exit_policy, config=cfg)
        pos = 0
        for ch in chunks:
            r.extend(candles[:pos + ch])
            pos += ch
        assert pos == len(candles), name
        got = r.finalize()
        assert got.fingerprint() == ref_fp, f"{name} fingerprint"
        assert _norm_ledger(got) == (ref_t, ref_a), f"{name} ledger"
        assert r.exit_coverage == ref_cov, f"{name} coverage"


def patterns(n):
    return {
        "bar-by-bar": [1] * n,
        "chunks-13": [13] * (n // 13) + ([n % 13] if n % 13 else []),
        "all-at-once": [n],
        "one-then-rest": [1, n - 1],
    }


def test_plain_fixed_sltp():
    c = mkcandles(220)
    e = sigs_at(c, [(10, "BUY"), (50, "SELL"), (100, "SELL"), (150, "BUY")])
    x = sigs_at(c, [(30, "SELL"), (120, "BUY")])
    check(c, e, x, FixedSlTpPolicy(stop_pct=0.01, target_pct=0.02),
          EngineConfig(figi="T"), patterns(len(c)))


def test_partials_be_abort():
    c = mkcandles(260)
    e = sigs_at(c, [(10, "BUY"), (60, "SELL"), (140, "BUY")])
    x = sigs_at(c, [(45, "SELL"), (200, "BUY")])
    cfg = EngineConfig(figi="T", wick_tol=0.05, be_trigger_r=1.0, be_offset_pct=0.0,
                       abort_r=0.5, abort_max_bars=5, partial_r=1.0,
                       partial_fraction=0.5, partial_to_be=True)
    check(c, e, x, AtrStopPolicy(period=14, multiplier=2.0, risk_reward=2.0),
          cfg, patterns(len(c)))


def test_neutral_semiflip_and_gate():
    c = mkcandles(220)
    e = sigs_at(c, [(10, "BUY"), (80, "SELL"), (150, "BUY")])
    x = sigs_at(c, [(60, "SELL"), (170, "SELL")])
    rows = regime_rows(c, [(0, "TREND_UP")] + [(i, "NEUTRAL") for i in (50, 51, 52, 120, 121, 122)])
    for mode in ("semi_flip", "gate"):
        cfg = EngineConfig(figi="T", neutral_mode=mode, regime_bars=list(rows))
        check(c, e, x, FixedSlTpPolicy(stop_pct=0.02, target_pct=0.03),
              cfg, patterns(len(c)))


def test_confirms_and_cooldowns():
    c = mkcandles(220)
    e = sigs_at(c, [(10, "BUY"), (12, "BUY"), (90, "SELL"), (150, "BUY")])
    x = sigs_at(c, [(60, "SELL"), (100, "BUY")])
    sp = SignalPolicyConfig(entry_confirm_bars=2, exit_confirm_window_bars=3,
                            confirm_flip=True, same_side_reentry_cooldown_bars=5)
    cfg = EngineConfig(figi="T", signal_policy=sp)
    check(c, e, x, FixedSlTpPolicy(stop_pct=0.02, target_pct=0.04),
          cfg, patterns(len(c)))


def test_session_force_flat():
    c = mkcandles(200)
    e = sigs_at(c, [(10, "BUY"), (100, "SELL")])
    x = sigs_at(c, [(150, "BUY")])
    sess = SessionPolicyConfig(overnight=False, force_flat_at_session_end=True)
    cfg = EngineConfig(figi="T", session_policy=sess)
    check(c, e, x, FixedSlTpPolicy(stop_pct=0.05, target_pct=0.1),
          cfg, patterns(len(c)))


def test_short_disabled_and_trailing():
    from app.engine.exits import AtrTrailingPolicy
    c = mkcandles(220)
    e = sigs_at(c, [(10, "SELL"), (80, "BUY"), (150, "SELL")])
    x = sigs_at(c, [(60, "BUY")])
    cfg = EngineConfig(figi="T", allow_short=False)
    check(c, e, x, AtrTrailingPolicy(period=14, initial_stop_atr=2.0),
          cfg, patterns(len(c)))


def test_single_bar_degenerate():
    c = mkcandles(1)
    e = sigs_at(c, [(0, "BUY")])
    ref = EngineRunner(strategy=ReplayStrategy(e),
                       exit_policy=FixedSlTpPolicy(stop_pct=0.01, target_pct=0.02),
                       config=EngineConfig(figi="T")).run(list(c))
    r = IncrementalRunner(strategy=ReplayStrategy(e),
                          exit_policy=FixedSlTpPolicy(stop_pct=0.01, target_pct=0.02),
                          config=EngineConfig(figi="T"))
    r.extend(c)
    got = r.finalize()
    assert got.fingerprint() == ref.fingerprint()
    assert _norm_ledger(got) == _norm_ledger(ref)


def test_replay_extend_order_same_ts():
    """extend() сохраняет batch-порядок: entry раньше exit на одном ts."""
    from app.engine.wave1 import ReplayStrategy as RS
    c = mkcandles(50)
    a = RS([(c[10].ts, "BUY")])
    a.extend([], exits=[(c[10].ts, "SELL")])
    b = RS([(c[10].ts, "BUY")], exits=[(c[10].ts, "SELL")])
    assert [(t.isoformat(), s.value, k) for t, s, k in a._pending] == \
           [(t.isoformat(), s.value, k) for t, s, k in b._pending]
    a.extend([(c[20].ts, "SELL")])
    got = []
    while a._pending:
        got.append(a._pending.popleft()[0])
    assert got == sorted(got)
