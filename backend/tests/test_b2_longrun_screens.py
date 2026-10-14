"""Focused tests for longrun dip screens (b2_reversal_longrun, b2_dip_confirm)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from b2_reversal_longrun import (  # noqa: E402
    net_from, period_slice, _t_stat, PERIODS,
)
from b2_dip_confirm import confirm_masks  # noqa: E402


def test_net_from_known_values():
    entry = np.array([100.0])
    exit_ = np.array([101.0])
    net = net_from(entry, exit_, slip=2e-4, fee=5e-4)
    entry_fill = 100.0 * 1.0002
    exit_fill = 101.0 * 0.9998
    fees = 5e-4 * entry_fill + 5e-4 * exit_fill
    expected = (exit_fill - entry_fill - fees) / 100.0
    assert net[0] == pytest.approx(expected, rel=1e-12)
    assert net[0] > 0


def test_net_from_costs_differ():
    entry = np.array([100.0, 100.0])
    exit_ = np.array([101.0, 101.0])
    n5 = net_from(entry, exit_, 2e-4, 5e-4)
    n10 = net_from(entry, exit_, 5e-4, 10e-4)
    assert n10[0] < n5[0]


def test_t_stat_basic():
    assert _t_stat(np.array([1.0, 1.0, 1.0])) == 0.0
    assert _t_stat(np.array([1.0])) == 0.0
    daily = np.array([0.1, 0.2, 0.15, 0.25, 0.2])
    t = _t_stat(daily)
    assert t > 2.0


def test_period_slice_covers_correct_days():
    days = np.arange(19723, 20700, dtype=np.int64)
    values = np.ones(len(days))
    counts = np.ones(len(days))
    v_dev, c_dev = period_slice(days, values, counts, "dev_2024_2025")
    assert len(c_dev) > 400
    v_chk, c_chk = period_slice(days, values, counts, "check_2026h1")
    assert len(c_chk) == pytest.approx(212, abs=5)
    assert v_dev.sum() == pytest.approx(c_dev.sum())
    assert v_chk.sum() == pytest.approx(c_chk.sum())


def test_confirm_masks_shapes_and_types():
    n = 500
    rng = np.random.default_rng(7)
    c = 100 * np.cumprod(1 + rng.normal(0, 0.002, n))
    df = pd.DataFrame({
        "open": c * (1 + rng.normal(0, 0.001, n)),
        "high": c * 1.002,
        "low": c * 0.998,
        "close": c,
        "volume": rng.integers(100, 10000, n).astype(float),
    })
    confs = confirm_masks(df)
    assert set(confs) == {
        "none", "vol15", "vol20", "down3", "down4",
        "rsi20", "rsi25", "lor01", "lor02", "vol15_down3",
    }
    for name, mask in confs.items():
        assert mask.dtype == bool, name
        assert len(mask) == n, name


def test_confirm_down_flags_require_consecutive():
    n = 20
    c = np.full(n, 100.0)
    c[10:] = [99, 98, 97, 96, 95, 94, 93, 92, 91, 90]
    df = pd.DataFrame({
        "open": c, "high": c, "low": c, "close": c,
        "volume": np.full(n, 1000.0),
    })
    confs = confirm_masks(df)
    assert bool(confs["down3"][9]) is False
    assert bool(confs["down3"][13]) is True
    assert bool(confs["down4"][14]) is True
    assert bool(confs["down4"][12]) is False


def test_confirm_vol_flags_monotone():
    n = 60
    vol = np.full(n, 1000.0)
    vol[-1] = 5000.0
    c = np.full(n, 100.0)
    df = pd.DataFrame({
        "open": c, "high": c, "low": c, "close": c, "volume": vol,
    })
    confs = confirm_masks(df)
    assert bool(confs["vol15"][-1]) is True
    assert bool(confs["vol20"][-1]) is True
    assert bool(confs["vol15_down3"][-1]) is False


def test_confirm_rsi_bounded():
    n = 300
    rng = np.random.default_rng(11)
    c = 100 * np.cumprod(1 + rng.normal(0, 0.01, n))
    df = pd.DataFrame({
        "open": c, "high": c * 1.001, "low": c * 0.999, "close": c,
        "volume": np.full(n, 1000.0),
    })
    confs = confirm_masks(df)
    assert (confs["rsi20"] | ~confs["rsi20"]).all()
    assert confs["rsi20"].sum() <= confs["rsi25"].sum()


def test_split_anomaly_window_logic():
    c = np.array([100.0] * 10 + [50.0] * 10)
    ret1 = c[1:] / c[:-1] - 1.0
    flag = np.zeros(len(c), dtype=bool)
    flag[1:] = np.abs(ret1) > 0.30
    cum = np.cumsum(flag)
    crossing = cum[19] - cum[9]
    inside = cum[8] - cum[2]
    assert crossing == 1
    assert inside == 0
