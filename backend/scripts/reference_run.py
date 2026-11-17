#!/usr/bin/env python3
"""REFERENCE RUN — эталонный прогон канонического движка (fingerprint).

REF-001: фиксированные dataset/стратегия/исполнение → EngineRunner → TradeLedger →
fingerprint. `--write` фиксирует эталон (configs/reference/REF-001.json), `--check`
ловит регрессии. `--compare-runtime` сверяет сделки движка с проектным replay
(таблица sandbox_trades) — REF-001b: сверка контуров.

Запуск (на .7 с БД):
  .venv/bin/python scripts/reference_run.py --write
  .venv/bin/python scripts/reference_run.py --check
  .venv/bin/python scripts/reference_run.py --dump-trades reports/reference/REF-001.engine_trades.json
  .venv/bin/python scripts/reference_run.py --compare-runtime "ref-001b-runtime 20260930-2200"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.engine import EngineConfig, EngineRunner  # noqa: E402
from app.engine.costs import CostModel  # noqa: E402
from app.engine.strategies import build_strategy  # noqa: E402

import ose_exit_matrix as oem  # noqa: E402

REF_PATH = ROOT / "configs" / "reference" / "REF-001.json"
DEFAULT_REF = {
    "id": "REF-001",
    "name": "canonical engine · rsi_trade_hub 10m SBER · signal-only exits",
    "dataset": {"source": "db", "ticker": "SBER", "interval": "10min",
                "period": ["2026-09-01", "2026-09-08"]},
    "strategy": {"engine": "rsi_trade_hub", "params": {}},
    "exit": {"policy": "signal_only"},
    "execution": {"qty": 1, "commission": 0.0005, "slippage_bps": 2.0},
}


def _figi(ticker: str) -> str:
    eng = create_engine(get_settings().database_url.replace("+asyncpg", ""), pool_pre_ping=True)
    with eng.connect() as c:
        f = c.execute(text("SELECT figi FROM instruments WHERE upper(ticker)=:t LIMIT 1"),
                      {"t": ticker.upper()}).scalar()
    if not f:
        raise SystemExit(f"нет figi для {ticker}")
    return str(f)


def _data_hash(bars) -> str:
    h = hashlib.sha256()
    for b in bars:
        h.update(f"{b.ts.isoformat()}|{b.open}|{b.high}|{b.low}|{b.close}|{b.volume}\n".encode())
    return h.hexdigest()[:16]


def _trade_dict(t) -> dict:
    return {
        "side": str(t.side), "entry_time": t.entry_time.isoformat(),
        "entry_price": round(float(t.entry_price), 6),
        "exit_time": t.exit_time.isoformat(), "exit_price": round(float(t.exit_price), 6),
        "exit_reason": str(t.exit_reason), "net": round(float(t.net_pnl), 4),
    }


def _strategy_hash(ref: dict) -> str:
    payload = json.dumps({"engine": ref["strategy"]["engine"],
                          "params": ref["strategy"].get("params") or {},
                          "exit": ref.get("exit") or {},
                          "execution": ref.get("execution") or {}},
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _git_commit() -> str:
    try:
        import subprocess
        out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _archive(path: Path, *, stamp: str) -> str | None:
    """Сохранить прежнюю версию эталона рядом, в archive/, прежде чем перезаписать.

    Эталон — точка отсчёта регрессий, поэтому его нельзя молча заменить: прежняя
    версия остаётся на диске со своим временем, а новая ссылается на неё в lineage.
    """
    if not path.exists():
        return None
    arc = path.parent / "archive"
    arc.mkdir(parents=True, exist_ok=True)
    target = arc / f"{path.stem}-{stamp}.json"
    target.write_bytes(path.read_bytes())
    return str(target.relative_to(path.parent))


def run_reference(ref: dict) -> dict:
    """Детерминированный эталонный прогон: DB (фикс. данные) → EngineRunner → ledger.

    warmup_days: движок грузит историю за N дней ДО периода (как проектный replay,
    который прогревает стратегию preload'ом), прогоняется по расширенному ряду, а в
    результат попадают сделки ТОЛЬКО окна [dfrom..dto] (pre_window_trades — счётчик
    прогpевочных сделок). Это выравнивает контуры движок↔runtime (REF-001b).
    """
    ds = ref["dataset"]
    figi = _figi(ds["ticker"])
    tf_s = oem.TF_SECONDS[ds["interval"]]
    warm = int(ref.get("warmup_days", 0) or 0)
    start_ts = datetime.fromisoformat(ds["period"][0] + "T00:00:00+00:00")
    load_from = (start_ts - timedelta(days=warm)).date().isoformat()
    _rows, bars = oem._load_tf_cached(figi, load_from, ds["period"][1], tf_s)
    strat = build_strategy(ref["strategy"]["engine"], ref["strategy"].get("params") or None)
    ex = ref["execution"]
    cfg = EngineConfig(
        figi=figi, qty=int(ex["qty"]),
        cost_model=CostModel(commission_rate=float(ex["commission"]),
                             slippage_bps=float(ex["slippage_bps"])),
    )
    led = EngineRunner(strategy=strat, exit_policy=oem.NoExitPolicy(), config=cfg).run(bars)
    trades = led.trades
    win = [t for t in trades if t.entry_time >= start_ts]
    window_bars = [b for b in bars if b.ts >= start_ts]
    return {
        "data_hash": _data_hash(bars),
        "strategy_hash": _strategy_hash(ref),
        "bars": len(bars),
        "bars_window": len(window_bars),
        "first_ts": bars[0].ts.isoformat() if bars else "",
        "last_ts": bars[-1].ts.isoformat() if bars else "",
        "load_from": load_from,
        "pre_window_trades": len(trades) - len(win),
        "trades": len(win),
        "net": round(sum(t.net_pnl for t in win), 4),
        "fingerprint": led.fingerprint(),
        "trades_list": [_trade_dict(t) for t in win],
    }


def _norm_side(s: str) -> str:
    s = str(s or "").upper()
    if s in ("BUY", "LONG"):
        return "LONG"
    if s in ("SELL", "SHORT"):
        return "SHORT"
    return s


def _runtime_trades(test_name: str, ticker: str = "") -> list[dict]:
    eng = create_engine(get_settings().database_url.replace("+asyncpg", ""), pool_pre_ping=True)
    sql = ("SELECT side, entry_time, entry_price, exit_time, exit_price, exit_reason, net_pnl "
           "FROM sandbox_trades WHERE test_name=:n")
    params = {"n": test_name}
    if ticker:
        sql += " AND upper(ticker)=:t"
        params["t"] = ticker.upper()
    sql += " ORDER BY entry_time, id"
    with eng.connect() as c:
        rows = c.execute(text(sql), params).fetchall()
    out = []
    for r in rows:
        et, xt = r[1], r[3]
        out.append({
            "side": _norm_side(r[0]), "entry_time": et.isoformat() if et else "",
            "entry_price": round(float(r[2] or 0), 6),
            "exit_time": xt.isoformat() if xt else "", "exit_price": round(float(r[4] or 0), 6),
            "exit_reason": str(r[5] or ""), "net": round(float(r[6] or 0), 4),
        })
    return out


def compare_runtime(engine: list[dict], runtime: list[dict],
                    time_tol_sec: float = 60.0, price_tol_pct: float = 0.05) -> list[str]:
    """REF-001b: сверка сроков/цен/сторон (qty и net не сравниваем — сайзинг контуров разный)."""
    problems: list[str] = []
    if len(engine) != len(runtime):
        problems.append(f"сделок: engine {len(engine)} vs runtime {len(runtime)}")
    for i, (e, r) in enumerate(zip(engine, runtime)):
        tag = f"#{i + 1} {e['entry_time'][:16]}"
        if _norm_side(e["side"]) != _norm_side(r["side"]):
            problems.append(f"{tag}: сторона {_norm_side(e['side'])} vs {_norm_side(r['side'])}")
        try:
            dt = abs((datetime.fromisoformat(r["entry_time"]) - datetime.fromisoformat(e["entry_time"])).total_seconds())
            if dt > time_tol_sec:
                problems.append(f"{tag}: вход Δ{dt / 60:.0f} мин (runtime {r['entry_time'][:16]})")
        except Exception:
            pass
        for f in ("entry_price", "exit_price"):
            if e[f] and r[f]:
                d = abs(e[f] - r[f]) / e[f] * 100
                if d > price_tol_pct:
                    problems.append(f"{tag}: {f} Δ{d:.3f}% ({e[f]:.2f} vs {r[f]:.2f})")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="зафиксировать эталон")
    ap.add_argument("--check", action="store_true", help="сверить с эталоном (регрессия)")
    ap.add_argument("--dump-trades", default="", help="выгрузить сделки движка в JSON")
    ap.add_argument("--compare-runtime", default="", help="имя проектного теста (sandbox_trades)")
    ap.add_argument("--runtime-ticker", default="", help="тикер runtime-фильтра (пусто = тикер датасета)")
    ap.add_argument("--file", default=str(REF_PATH))
    ap.add_argument("--reason", default="", help="причина новой версии эталона (в lineage)")
    a = ap.parse_args()
    path = Path(a.file)
    ref = json.loads(path.read_text(encoding="utf-8")) if path.exists() else dict(DEFAULT_REF)
    got = run_reference(ref)
    print(f"{ref['id']}: баров {got['bars']} · сделок {got['trades']} · net {got['net']:+.4f} · "
          f"fingerprint {got['fingerprint']}")
    if a.dump_trades:
        out = Path(a.dump_trades)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"ref_id": ref["id"], "trades": got["trades_list"]},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"сделки движка: {out} ({len(got['trades_list'])})")
    if a.compare_runtime:
        tk = a.runtime_ticker or ref["dataset"]["ticker"]
        rt = _runtime_trades(a.compare_runtime, tk)
        print(f"runtime '{a.compare_runtime}' [{tk}]: сделок {len(rt)}")
        probs = compare_runtime(got["trades_list"], rt)
        if probs:
            print(f"РАСХОЖДЕНИЯ ({len(probs)}):")
            for p in probs[:20]:
                print("  -", p)
            return 1
        print("OK: сроки/цены/стороны совпадают (в допусках)")
        return 0
    exp = ref.get("expected")
    if a.check:
        if not exp:
            print("нет expected в эталоне — сначала --write")
            return 2
        keys = ("data_hash", "bars", "pre_window_trades", "trades", "net", "fingerprint")
        bad = [k for k in keys if exp.get(k) != got.get(k)]
        if bad:
            print("РЕГРЕССИЯ:", ", ".join(bad))
            for k in bad:
                print(f"  {k}: expected {exp.get(k)!r} got {got.get(k)!r}")
            return 1
        print("OK: эталон совпадает")
        return 0
    if a.write or not exp:
        exp_got = {k: got[k] for k in ("data_hash", "bars", "pre_window_trades",
                                       "trades", "net", "fingerprint")}
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        prev = _archive(path, stamp=stamp)
        ds = ref["dataset"]
        ref["expected"] = exp_got
        ref["lineage"] = {
            "created_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "host": os.environ.get("COMPUTERNAME", ""),
            "git_commit": _git_commit(),
            "reason": a.reason or "первичная фиксация",
            "previous": prev,
            "inputs": {
                "ticker": ds["ticker"],
                "figi": _figi(ds["ticker"]),
                "interval": ds["interval"],
                "window_utc": [ds["period"][0] + "T00:00:00+00:00",
                               (datetime.fromisoformat(ds["period"][1])
                                + timedelta(days=1)).date().isoformat() + "T00:00:00+00:00"],
                "warmup_days": int(ref.get("warmup_days", 0) or 0),
                "load_from": got["load_from"],
                "bars": got["bars"],
                "bars_window": got["bars_window"],
                "first_ts": got["first_ts"],
                "last_ts": got["last_ts"],
                "data_hash": got["data_hash"],
                "strategy_hash": got["strategy_hash"],
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ref, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved: {path}" + (f" (архив прежней версии: {prev})" if prev else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
