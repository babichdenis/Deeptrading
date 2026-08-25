"""Расчёт «потолка» торговли: идеальный трейдер со знанием будущего.

Используется во вкладке «Тест»: визуальный анализ того, сколько
максимально можно заработать на краткосрочной торговле инструментом,
и какая доля сигналов входа/выхода предсказуема заранее.
"""
from __future__ import annotations

import numpy as np


def zigzag_swings(candles: list[dict], min_move_pct: float) -> list[dict]:
    """Подтверждённый зигзаг. Возвращает точки (kind, price, idx, conf_idx)."""
    n = len(candles)
    high_idx = low_idx = 0
    swings: list[dict] = []
    trend = 0  # 0 неизвестно, 1 ждём high, -1 ждём low

    def low_of(i: int) -> float:
        return float(candles[i]["low"])

    def high_of(i: int) -> float:
        return float(candles[i]["high"])

    for i in range(n):
        if trend == 0:
            if low_of(i) < low_of(low_idx):
                low_idx = high_idx = i
            if high_of(i) > high_of(high_idx):
                high_idx = low_idx = i
            if high_of(i) - low_of(low_idx) >= low_of(low_idx) * min_move_pct:
                swings.append({"kind": "low", "price": low_of(low_idx), "idx": low_idx, "conf": i})
                high_idx = i
                trend = 1
            elif high_of(high_idx) - low_of(i) >= high_of(high_idx) * min_move_pct:
                swings.append({"kind": "high", "price": high_of(high_idx), "idx": high_idx, "conf": i})
                low_idx = i
                trend = -1
        elif trend == 1:
            if high_of(i) > high_of(high_idx):
                high_idx = i
            if high_of(high_idx) - low_of(i) >= high_of(high_idx) * min_move_pct:
                swings.append({"kind": "high", "price": high_of(high_idx), "idx": high_idx, "conf": i})
                low_idx = i
                trend = -1
        else:
            if low_of(i) < low_of(low_idx):
                low_idx = i
            if high_of(i) - low_of(low_idx) >= low_of(low_idx) * min_move_pct:
                swings.append({"kind": "low", "price": low_of(low_idx), "idx": low_idx, "conf": i})
                high_idx = i
                trend = 1
    return swings


def simulate_perfect_swing(
    candles: list[dict],
    fee_rate: float,
    capital: float,
    lot: int,
    min_move_pct: float,
    slippage: float,
    multiday: bool = False,
) -> dict:
    """Идеальный свинг-трейдер: покупает на каждой подтверждённой волне зигзага."""
    if multiday:
        groups: list[list[dict]] = [candles]
    else:
        groups = []
        for c in candles:
            if not groups or groups[-1][0]["ts"][:10] != c["ts"][:10]:
                groups.append([])
            groups[-1].append(c)

    trades: list[dict] = []
    for g in groups:
        swings = zigzag_swings(g, min_move_pct)
        if not swings:
            continue
        pos = 0  # >0 лонг в лотах, <0 шорт в лотах
        entry_px = None
        entry_ts = None
        side_name = None
        cash = capital
        for sw in swings:
            px = sw["price"]
            ts = g[sw["idx"]]["ts"]
            if sw["kind"] == "low" and pos == 0:
                lots = int(capital / (px * (1 + slippage) * lot * (1 + fee_rate)))
                if lots > 0:
                    pos = lots
                    cash -= lots * lot * px * (1 + slippage) * (1 + fee_rate)
                    entry_px, entry_ts, side_name = px, ts, "LONG"
            elif sw["kind"] == "high" and pos == 0:
                lots = int(capital / (px * (1 + slippage) * lot * (1 + fee_rate)))
                if lots > 0:
                    pos = -lots
                    cash += lots * lot * px * (1 - slippage) * (1 - fee_rate)
                    entry_px, entry_ts, side_name = px, ts, "SHORT"
            elif sw["kind"] == "high" and pos > 0:
                # закрыть LONG и сразу открыть SHORT (флип)
                exit_px = px
                cash += pos * lot * exit_px * (1 - slippage) * (1 - fee_rate)
                pnl = pos * lot * (
                    exit_px * (1 - slippage) * (1 - fee_rate)
                    - entry_px * (1 + slippage) * (1 + fee_rate)
                )
                trades.append(
                    {
                        "side": side_name,
                        "entry_ts": entry_ts,
                        "exit_ts": ts,
                        "entry_px": entry_px,
                        "exit_px": exit_px,
                        "pnl": round(pnl, 2),
                        "lots": abs(pos),
                    }
                )
                lots = int(capital / (px * (1 + slippage) * lot * (1 + fee_rate)))
                if lots > 0:
                    pos = -lots
                    cash += lots * lot * px * (1 - slippage) * (1 - fee_rate)
                    entry_px, entry_ts, side_name = px, ts, "SHORT"
            elif sw["kind"] == "low" and pos < 0:
                # закрыть SHORT и сразу открыть LONG (флип)
                exit_px = px
                cash -= abs(pos) * lot * exit_px * (1 + slippage) * (1 + fee_rate)
                pnl = abs(pos) * lot * (
                    entry_px * (1 - slippage) * (1 - fee_rate)
                    - exit_px * (1 + slippage) * (1 + fee_rate)
                )
                trades.append(
                    {
                        "side": side_name,
                        "entry_ts": entry_ts,
                        "exit_ts": ts,
                        "entry_px": entry_px,
                        "exit_px": exit_px,
                        "pnl": round(pnl, 2),
                        "lots": abs(pos),
                    }
                )
                lots = int(capital / (px * (1 + slippage) * lot * (1 + fee_rate)))
                if lots > 0:
                    pos = lots
                    cash -= lots * lot * px * (1 + slippage) * (1 + fee_rate)
                    entry_px, entry_ts, side_name = px, ts, "LONG"
        if pos > 0:
            close_px = float(g[-1]["close"])
            cash += pos * lot * close_px * (1 - slippage) * (1 - fee_rate)
            pnl = pos * lot * (
                close_px * (1 - slippage) * (1 - fee_rate)
                - entry_px * (1 + slippage) * (1 + fee_rate)
            )
            trades.append(
                {
                    "side": side_name,
                    "entry_ts": entry_ts,
                    "exit_ts": g[-1]["ts"],
                    "entry_px": entry_px,
                    "exit_px": close_px,
                    "pnl": round(pnl, 2),
                    "lots": pos,
                    "open": True,
                }
            )
        elif pos < 0:
            close_px = float(g[-1]["close"])
            cash -= abs(pos) * lot * close_px * (1 + slippage) * (1 + fee_rate)
            pnl = abs(pos) * lot * (
                entry_px * (1 - slippage) * (1 - fee_rate)
                - close_px * (1 + slippage) * (1 + fee_rate)
            )
            trades.append(
                {
                    "side": side_name,
                    "entry_ts": entry_ts,
                    "exit_ts": g[-1]["ts"],
                    "entry_px": entry_px,
                    "exit_px": close_px,
                    "pnl": round(pnl, 2),
                    "lots": abs(pos),
                    "open": True,
                }
            )
    longs = [t for t in trades if t.get("side") == "LONG"]
    shorts = [t for t in trades if t.get("side") == "SHORT"]
    total_pnl = sum(t["pnl"] for t in trades)
    return {"trades": trades, "pnl": round(total_pnl, 2),
            "longs": len(longs), "shorts": len(shorts),
            "long_pnl": round(sum(t["pnl"] for t in longs), 2),
            "short_pnl": round(sum(t["pnl"] for t in shorts), 2)}


def ceiling_per_candle(candles: list[dict], fee_rate: float, lot: int, capital: float) -> dict:
    """Абсолютный потолок: на каждом баре купить по low, продать по high (1 лот).
    С 1 лотом — консервативная нижняя граница потолка, достижимая без переиспользования
    капитала внутри бара.
    """
    total = 0.0
    profitable = 0
    for c in candles:
        buy_val = float(c["low"]) * lot
        sell_val = float(c["high"]) * lot
        p = (sell_val - buy_val) - buy_val * fee_rate - sell_val * fee_rate
        if p > 0:
            total += p
            profitable += 1
    return {"pnl": round(total, 2), "bars": len(candles), "profitable_bars": profitable}


def buy_and_hold(candles: list[dict], fee_rate: float, capital: float, lot: int) -> dict:
    if not candles:
        return {"pnl": 0, "pct": 0.0, "lots": 0}
    first = float(candles[0]["open"])
    last = float(candles[-1]["close"])
    lots = int(capital / (first * lot * (1 + fee_rate)))
    if lots <= 0:
        return {"pnl": 0, "pct": 0.0, "lots": 0}
    buy = lots * lot * first * (1 + fee_rate)
    sell = lots * lot * last * (1 - fee_rate)
    return {"pnl": round(sell - buy, 2), "pct": round((sell - buy) / buy * 100, 2), "lots": lots}


def _rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    delta = np.diff(closes, prepend=closes[0])
    up = np.where(delta > 0, delta, 0.0)
    down = np.where(delta < 0, -delta, 0.0)
    roll = lambda a: np.convolve(a, np.ones(period) / period, mode="full")[: len(a)]
    ru = roll(up)
    rd = roll(down)
    rs = np.divide(ru, rd, out=np.full_like(ru, np.inf), where=rd > 1e-12)
    rsi = 100 - 100 / (1 + rs)
    rsi[:period] = 50.0
    return rsi


def predictability(candles: list[dict], min_move_pct: float, window: int = 10) -> dict:
    """Какая доля сигналов зигзага предсказуема заранее индикаторами."""
    n = len(candles)
    closes = np.array([float(c["close"]) for c in candles])
    lows = np.array([float(c["low"]) for c in candles])
    highs = np.array([float(c["high"]) for c in candles])
    vols = np.array([float(c["volume"]) for c in candles])

    swings = zigzag_swings(candles, min_move_pct)

    low_flags = np.zeros(n)
    high_flags = np.zeros(n)
    lags = []
    for sw in swings:
        lag = sw["conf"] - sw["idx"]
        lags.append(lag)
        a = max(0, sw["idx"] - window)
        b = min(n, sw["idx"] + window)
        if sw["kind"] == "low":
            low_flags[a:b] = 1
        else:
            high_flags[a:b] = 1

    # доля движения, прошедшая к моменту подтверждения
    captured = []
    for k in range(len(swings) - 1):
        sw = swings[k]
        nxt = swings[k + 1]
        conf_px = lows[sw["conf"]] if sw["kind"] == "low" else highs[sw["conf"]]
        total_move = abs(nxt["price"] - sw["price"])
        if total_move > 0:
            captured.append((total_move - abs(conf_px - sw["price"])) / total_move * 100)

    # индикаторы
    rsi14 = _rsi(closes)
    ema_f = _ema(closes, 8)
    ema_s = _ema(closes, 21)
    mom5 = np.zeros(n)
    mom5[5:] = (closes[5:] - closes[:-5]) / np.maximum(closes[:-5], 1e-9)
    rules: list[dict] = []

    def eval_rule(name: str, pred: np.ndarray, flags: np.ndarray) -> dict:
        sig = pred.astype(int)
        hits = int((sig * flags).sum())
        prec = hits / sig.sum() if sig.sum() else 0
        cov = hits / flags.sum() if flags.sum() else 0
        base = float(flags.mean()) * 100
        return {
            "name": name,
            "signals": int(sig.sum()),
            "precision": round(prec * 100, 1),
            "coverage": round(cov * 100, 1),
            "base": round(base, 1),
        }

    rules.append(eval_rule("Момент 5м < -0.4%", mom5 < -0.004, low_flags))
    rules.append(eval_rule("RSI < 25", rsi14 < 25, low_flags))
    rules.append(eval_rule("RSI < 30", rsi14 < 30, low_flags))
    rules.append(eval_rule("RSI > 75", rsi14 > 75, high_flags))
    rules.append(eval_rule("RSI > 70", rsi14 > 70, high_flags))

    # walk-forward логистическая регрессия (50/50 по времени)
    feats = np.column_stack(
        [
            rsi14,
            np.clip((closes - np.minimum(highs, lows)) / np.maximum(highs - lows, 1e-9), 0, 1),
            mom5,
            _ema(closes, 8) / np.maximum(_ema(closes, 21), 1e-9) - 1,
        ]
    )
    split = int(n * 0.5)
    wf: dict[str, dict] = {}
    for label, flags in [("entry", low_flags), ("exit", high_flags)]:
        Xtr = feats[:split]
        Xte = feats[split:]
        ytr = flags[:split].astype(float)
        yte = flags[split:].astype(float)
        if Xtr.shape[0] < 100 or ytr.sum() < 20 or yte.sum() < 20:
            wf[label] = {"precision": None, "coverage": None, "base": round(float(yte.mean()) * 100, 1)}
            continue
        proba = _logreg(Xtr, ytr, Xte)
        best = {"prec": 0.0, "cov": 0.0, "cut": 0.5}
        for cut in [0.3, 0.4, 0.5, 0.6]:
            pred = (proba > cut).astype(int)
            hits = int((pred * yte).sum())
            if pred.sum() == 0 or yte.sum() == 0:
                continue
            prec = hits / pred.sum()
            cov = hits / yte.sum()
            if prec + cov > best["prec"] + best["cov"]:
                best = {"prec": prec, "cov": cov, "cut": cut}
        wf[label] = {
            "precision": round(best["prec"] * 100, 1),
            "coverage": round(best["cov"] * 100, 1),
            "cut": best["cut"],
            "base": round(float(yte.mean()) * 100, 1),
        }

    return {
        "swings": len(swings),
        "lows": sum(1 for s in swings if s["kind"] == "low"),
        "highs": sum(1 for s in swings if s["kind"] == "high"),
        "confirm_lag_min_median": int(np.median(lags)) if lags else 0,
        "confirm_lag_min_mean": round(float(np.mean(lags)), 1) if lags else 0,
        "captured_median_pct": round(float(np.median(captured)), 1) if captured else 0,
        "rules": rules,
        "walkforward": wf,
    }


def _ema(values: np.ndarray, span: int) -> np.ndarray:
    alpha = 2 / (span + 1)
    out = np.empty_like(values)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def _logreg(Xtr: np.ndarray, ytr: np.ndarray, Xte: np.ndarray, lr: float = 0.5, epochs: int = 2000) -> np.ndarray:
    mu = Xtr.mean(0)
    sd = Xtr.std(0) + 1e-9
    Xtr = np.column_stack([np.ones(len(Xtr)), (Xtr - mu) / sd])
    Xte = np.column_stack([np.ones(len(Xte)), (Xte - mu) / sd])
    w = np.zeros(Xtr.shape[1])
    pos = ytr.mean()
    w[0] = np.log(pos / (1 - pos) + 1e-9)
    for _ in range(epochs):
        p = 1 / (1 + np.exp(-Xtr @ w))
        w += lr * Xtr.T @ (ytr - p) / len(ytr)
    return 1 / (1 + np.exp(-Xte @ w))
