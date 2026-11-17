"""Macro attribution with canonical Wilder ADX and point-in-time validation.

Historical v3 results use a different indicator and require a new run.
"""

import sys, os, json, csv
import math
import argparse
import hashlib
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from macro_regime_attribution_v2 import ema
from last_closed_bar import last_closed_bar

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.engine.indicatorhub import _adx
from app.engine.models import Candle
from app.config import get_settings

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(BACKEND, "reports")
DATA = os.path.join(BACKEND, "data")
TF = 5

FACTORS_CSV = {"BRENT": "brent_5m.csv", "GOLD": "gold_5m.csv",
               "USDRUB": "usdrub_5m.csv", "IMOEX": "imoex_5m.csv"}
# Те же ряды в базе (candles, interval=2 = 5 минут). База — источник истины:
# CSV в backend/data исторически склеивались дважды и содержат перемешанные строки.
FACTOR_FIGI = {"BRENT": "BRENT", "GOLD": "GOLD", "USDRUB": "USDRUB", "IMOEX": "BBG00KDWPPW2"}
INTERVAL_5M = 2

FIGI_SECTOR = {
    "BBG008F2T3T2": ("RUAL", "Metals/USD-RUB"), "BBG004S681B4": ("NLMK", "Metals/USD-RUB"),
    "BBG004S681M2": ("SNGSP", "OilGas/Brent"), "BBG004S683W7": ("AFLT", "IMOEX/risk"),
    "BBG004S68CP5": ("MVID", "IMOEX/risk"),
}
SECTOR_FACTOR = {"IMOEX/risk": "IMOEX", "OilGas/Brent": "BRENT", "Metals/USD-RUB": "USDRUB"}
METALS_GOLD = ("BBG008F2T3T2", "BBG004S681B4")

WINDOWS = {
    "July_2026": os.path.join(REPORTS, "5b44f3b383df", "trades.csv"),
    "H1_2026": os.path.join(REPORTS, "gold_window_202601_202607", "trades.csv"),
}

# фактор -> (группа-гипотеза "good", направление сравнения)
# GOLD/USDRUB: ожидается down>up; IMOEX/BRENT: ожидается up>down
GOOD_GROUP = {"GOLD": "down_trend", "USDRUB": "down_trend", "IMOEX": "up_trend", "BRENT": "up_trend"}


def load_full_csv(name, data_dir=None):
    path = os.path.join(data_dir or DATA, FACTORS_CSV[name])
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for line_number, line in enumerate(f, 1):
            if not line.strip():
                continue
            fields = line.strip().rstrip(";").split(";")
            if len(fields) != 6:
                raise ValueError(f"{path}:{line_number}: expected six fields")
            timestamp = datetime.fromisoformat(fields[0].replace("Z", "+00:00"))
            if timestamp.utcoffset() is None:
                raise ValueError(f"{path}:{line_number}: timezone is required")
            timestamp = timestamp.astimezone(timezone.utc)
            high, low, opening, close, volume = map(float, fields[1:])
            if not all(math.isfinite(value) for value in (high, low, opening, close, volume)):
                raise ValueError(f"{path}:{line_number}: non-finite OHLCV")
            if not (0 < low <= min(opening, close) <= max(opening, close) <= high and volume >= 0):
                raise ValueError(f"{path}:{line_number}: invalid OHLCV")
            if out and timestamp <= out[-1][0]:
                raise ValueError(f"{path}:{line_number}: timestamps must strictly increase")
            out.append((timestamp, opening, high, low, close, volume))
    return out


def _db_dsn() -> str:
    settings = get_settings()
    return (f"postgresql://{settings.postgres_user}:{settings.postgres_password}"
            f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}")


def load_full_db(name, dsn=None):
    """Загрузить ряд фактора из candles (interval=2). Те же проверки, что и для CSV.

    База — источник истины: CSV в data/ склеивались дважды. Ряд обязан быть строго
    возрастающим по времени, без дублей и с согласованными OHLCV.
    """
    import psycopg2

    figi = FACTOR_FIGI[name]
    out = []
    with psycopg2.connect(dsn or _db_dsn()) as db:
        with db.cursor(name=f"macro_{name.lower()}") as cur:
            cur.itersize = 20000
            cur.execute(
                "SELECT ts, open, high, low, close, volume FROM candles "
                "WHERE figi = %s AND interval = %s ORDER BY ts",
                (figi, INTERVAL_5M))
            for ts, opening, high, low, close, volume in cur:
                timestamp = ts.astimezone(timezone.utc)
                opening, high, low, close = (float(opening), float(high), float(low), float(close))
                volume = float(volume)
                if not all(math.isfinite(value) for value in (high, low, opening, close, volume)):
                    raise ValueError(f"{figi}: non-finite OHLCV at {timestamp}")
                if not (0 < low <= min(opening, close) <= max(opening, close) <= high and volume >= 0):
                    raise ValueError(f"{figi}: invalid OHLCV at {timestamp}")
                if out and timestamp <= out[-1][0]:
                    raise ValueError(f"{figi}: timestamps must strictly increase at {timestamp}")
                out.append((timestamp, opening, high, low, close, volume))
    if not out:
        raise ValueError(f"{figi}: no bars in candles (interval={INTERVAL_5M})")
    return out


def factor_fingerprint(name, bars) -> str:
    """Отпечаток ряда в базе: сверка входов до и после расчёта."""
    digest = hashlib.sha256()
    digest.update(f"{FACTOR_FIGI[name]}|{INTERVAL_5M}|{len(bars)}|".encode())
    for bar in bars:
        digest.update(f"{bar[0].isoformat()}|{bar[1]}|{bar[2]}|{bar[3]}|{bar[4]}|{bar[5]};".encode())
    return digest.hexdigest()


def regime_series(bars):
    closes = [b[4] for b in bars]
    ema_s = ema(closes, 50)
    candles = [
        Candle(ts=bar[0], open=bar[1], high=bar[2], low=bar[3], close=bar[4], volume=bar[5])
        for bar in bars
    ]
    adx = _adx(candles, 14)["adx"]
    return closes, ema_s, adx


def regime_B(j, closes, ema_s, adx):
    if j < 50 or j >= len(closes):
        return None
    a = adx[j]
    values = (a, closes[j], ema_s[j])
    if any(value is None or not math.isfinite(value) for value in values):
        return None
    if a <= 25 or closes[j] == ema_s[j]:
        return "flat"
    return "up_trend" if closes[j] > ema_s[j] else "down_trend"


def regime_at(timestamps, decision_time, series, max_age_minutes=None):
    if max_age_minutes is not None and (not math.isfinite(max_age_minutes) or max_age_minutes < 0):
        raise ValueError("max_age_minutes must be finite and nonnegative")
    index = last_closed_bar(timestamps, decision_time, TF)
    if index < 0:
        return None, "missing"
    age = decision_time - timestamps[index] - timedelta(minutes=TF)
    if max_age_minutes is not None and age > timedelta(minutes=max_age_minutes):
        return None, "stale"
    regime = regime_B(index, *series)
    return regime, "known" if regime is not None else "unavailable"


def perm_test(good, bad, n_perm=5000, seed=42):
    import numpy as np
    if isinstance(n_perm, bool) or not isinstance(n_perm, int) or n_perm < 1:
        raise ValueError("n_perm must be a positive integer")
    good = np.asarray(good, float)
    bad = np.asarray(bad, float)
    if good.ndim != 1 or bad.ndim != 1:
        raise ValueError("samples must be one-dimensional")
    if not np.isfinite(good).all() or not np.isfinite(bad).all():
        raise ValueError("samples must contain finite values")
    if len(good) < 30 or len(bad) < 30:
        return None
    actual = good.mean() - bad.mean()
    rng = np.random.default_rng(seed)
    g = np.r_[np.ones(len(good)), np.zeros(len(bad))]
    nets = np.r_[good, bad]
    perms = np.empty(n_perm)
    for i in range(n_perm):
        gi = rng.permutation(g)
        perms[i] = nets[gi == 1].mean() - nets[gi == 0].mean()
    p_two = float((np.count_nonzero(np.abs(perms) >= abs(actual)) + 1) / (n_perm + 1))
    p_one = float((np.count_nonzero(perms >= actual) + 1) / (n_perm + 1))
    return {"actual_diff": round(float(actual), 4), "p_two_sided": p_two,
            "p_one_sided": p_one,
            "n_perm": n_perm, "seed": seed, "p_estimator": "plus_one",
            "n_good": int(len(good)), "n_bad": int(len(bad)),
            "mean_good": round(float(good.mean()), 3), "mean_bad": round(float(bad.mean()), 3)}


def econ(sub):
    n = len(sub)
    if n == 0:
        return {"count": 0}
    net = sum(r["net"] for r in sub)
    gp = sum(max(r["net"], 0) for r in sub)
    gl = sum(max(-r["net"], 0) for r in sub)
    return {"count": n, "net": round(net, 2), "net_per_trade": round(net / n, 3),
            "pf": round(gp / gl, 3) if gl > 0 else None,
            "win_rate": round(sum(1 for r in sub if r["net"] > 0) / n, 4)}


def load_trades(path):
    with open(path, encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required = {"figi", "decision_time", "net_rub", "gross_rub"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"{path}: missing required trade columns")
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError(f"{path}: duplicate trade columns")
        trades = []
        seen_ids = set()
        for row_number, row in enumerate(reader, 2):
            if None in row or any(not row.get(field) for field in required):
                raise ValueError(f"{path}:{row_number}: malformed trade")
            timestamp = datetime.fromisoformat(row["decision_time"].replace("Z", "+00:00"))
            if timestamp.utcoffset() is None:
                raise ValueError(f"{path}:{row_number}: decision_time must be timezone-aware")
            row["decision_time"] = timestamp.astimezone(timezone.utc).isoformat()
            for field in ("net_rub", "gross_rub"):
                if not math.isfinite(float(row[field])):
                    raise ValueError(f"{path}:{row_number}: non-finite {field}")
            trade_id = row.get("trade_id")
            if trade_id:
                if trade_id in seen_ids:
                    raise ValueError(f"{path}:{row_number}: duplicate trade_id {trade_id}")
                seen_ids.add(trade_id)
            trades.append(row)
        return trades


def file_hashes(paths):
    return {str(Path(path).resolve()): hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in paths}


def save_results(result, output_dir):
    hpath = Path(REPORTS) / "h058_significance_testing.json"
    h = {"h058": {}}
    if hpath.exists():
        with hpath.open(encoding="utf-8") as source:
            h = json.load(source)
    july = result["July_2026"]
    alpha = result["bonferroni"]["alpha"]
    h["h058"]["1c"] = {
        "note": "macro_regimes July 2026, last-closed bar, full-series warmup",
        "method": result["method"], "bar_alignment": result["bar_alignment"],
        "July_2026": {name: july[name]["perm"] for name in july},
        "bonferroni_alpha": alpha,
    }
    h["h058"]["1c_macro_regimes"] = [
        f"{name}:{GOOD_GROUP[name]}_vs_{'down_trend' if GOOD_GROUP[name] == 'up_trend' else 'up_trend'}"
        for name in ("IMOEX", "BRENT", "GOLD", "USDRUB")
    ]
    payloads = {
        "macro_regime_attribution_v3.json": json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False),
        "h058_significance_testing.json": json.dumps(h, ensure_ascii=False, indent=2, allow_nan=False),
    }
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=False)
    for name, payload in payloads.items():
        with (destination / name).open("x", encoding="utf-8") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    completion = {"status": "complete", "outputs": file_hashes(destination / name for name in payloads)}
    with (destination / "completion.json").open("x", encoding="utf-8") as output:
        json.dump(completion, output, indent=2)
        output.flush()
        os.fsync(output.fileno())
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description="Recompute macro attribution into a new run directory.")
    parser.add_argument("--output-dir", required=True, help="New directory for this run")
    parser.add_argument("--data-dir", default=DATA, help="Validated factor CSV directory")
    parser.add_argument("--source", choices=("db", "csv"), default="db",
                        help="Where factor series come from: database candles (default) or CSV files")
    parser.add_argument("--july-trades", default=WINDOWS["July_2026"])
    parser.add_argument("--h1-trades", default=WINDOWS["H1_2026"])
    parser.add_argument("--max-factor-age-minutes", type=float, default=None,
                        help="Maximum elapsed minutes since factor bar close; omitted means unchecked")
    args = parser.parse_args(argv)
    if args.max_factor_age_minutes is not None and (
        not math.isfinite(args.max_factor_age_minutes) or args.max_factor_age_minutes < 0
    ):
        parser.error("--max-factor-age-minutes must be finite and nonnegative")
    windows = {"July_2026": args.july_trades, "H1_2026": args.h1_trades}
    if Path(args.output_dir).exists():
        parser.error("--output-dir must be a new directory")
    if args.source == "db":
        # Факторы читаются из базы; файлами сверяются только окна сделок.
        inputs = list(windows.values())
        input_hashes = file_hashes(inputs)
        factors = {n: load_full_db(n) for n in FACTORS_CSV}
        factor_hashes = {n: factor_fingerprint(n, bars) for n, bars in factors.items()}
    else:
        inputs = [Path(args.data_dir) / name for name in FACTORS_CSV.values()] + list(windows.values())
        input_hashes = file_hashes(inputs)
        factors = {n: load_full_csv(n, args.data_dir) for n in FACTORS_CSV}
        factor_hashes = {}
    print("loaded factors:", {n: len(v) for n, v in factors.items()}, "source:", args.source, flush=True)
    series = {n: regime_series(b) for n, b in factors.items()}
    idx = {n: [b[0] for b in factors[n]] for n in factors}

    result = {"schema": "macro_regime_attribution_v3", "method": "B: Wilder ADX14>25, close vs EMA50",
              "bar_alignment": "last_closed (ts+5min<=decision_time)",
              "warmup": "full supplied series", "note": "canonical ADX; historical results require recomputation",
              "input_hashes": input_hashes,
              "factor_source": args.source,
              "factor_hashes": factor_hashes,
              "code_hashes": file_hashes([__file__, Path(__file__).with_name("last_closed_bar.py"),
                                         Path(__file__).with_name("macro_regime_attribution_v2.py"),
                                         Path(BACKEND) / "app/engine/indicatorhub.py",
                                         Path(BACKEND) / "app/engine/models.py"]),
              "max_factor_age_minutes": args.max_factor_age_minutes,
              "freshness_checked": args.max_factor_age_minutes is not None,
              "factor_ranges": {
                  name: {"count": len(bars),
                         "first": bars[0][0].isoformat() if bars else None,
                         "last": bars[-1][0].isoformat() if bars else None}
                  for name, bars in factors.items()},
              "coverage": {}}

    for wname, wpath in windows.items():
        trades = load_trades(wpath)
        print(f"\n=== window {wname}: {len(trades)} trades ===", flush=True)
        rows = []
        for t in trades:
            figi = t["figi"]
            if figi not in FIGI_SECTOR:
                continue
            dt = datetime.fromisoformat(t["decision_time"].replace("Z", "+00:00"))
            ticker, sector = FIGI_SECTOR[figi]
            fname = SECTOR_FACTOR[sector]
            rg, status = regime_at(idx[fname], dt, series[fname], args.max_factor_age_minutes)
            rows.append({"ticker": ticker, "sector": sector, "factor": fname,
                         "regime": rg, "status": status, "net": float(t["net_rub"]), "gross": float(t["gross_rub"]),
                         "win": float(t["net_rub"]) > 0})
        # металлы + Gold доп.
        for t in trades:
            figi = t["figi"]
            if figi not in METALS_GOLD:
                continue
            dt = datetime.fromisoformat(t["decision_time"].replace("Z", "+00:00"))
            ticker, sector = FIGI_SECTOR[figi]
            rg, status = regime_at(idx["GOLD"], dt, series["GOLD"], args.max_factor_age_minutes)
            rows.append({"ticker": ticker, "sector": "Metals/Gold", "factor": "GOLD",
                         "regime": rg, "status": status, "net": float(t["net_rub"]), "gross": float(t["gross_rub"]),
                         "win": float(t["net_rub"]) > 0})

        result["coverage"][wname] = {
            "trades": len(trades),
            "unmapped_trades": sum(trade["figi"] not in FIGI_SECTOR for trade in trades),
            "factors": {
                name: {status: sum(row["factor"] == name and row["status"] == status for row in rows)
                       for status in ("known", "missing", "stale", "unavailable")}
                for name in FACTORS_CSV},
        }
        wres = {}
        for fname in ("IMOEX", "BRENT", "GOLD", "USDRUB"):
            good = GOOD_GROUP[fname]
            bad = "down_trend" if good == "up_trend" else "up_trend"
            g = [r["net"] for r in rows if r["factor"] == fname and r["regime"] == good]
            b = [r["net"] for r in rows if r["factor"] == fname and r["regime"] == bad]
            pt = perm_test(g, b)
            grp_good = econ([r for r in rows if r["factor"] == fname and r["regime"] == good])
            grp_bad = econ([r for r in rows if r["factor"] == fname and r["regime"] == bad])
            wres[fname] = {"good_group": good, "bad_group": bad, "perm": pt,
                           "econ_good": grp_good, "econ_bad": grp_bad}
            sig = pt and (pt["p_one_sided"] < 0.05 / len(FACTORS_CSV))
            print(f"  {fname}: {good} n={len(g)} net/t={grp_good.get('net_per_trade')} vs "
                  f"{bad} n={len(b)} net/t={grp_bad.get('net_per_trade')} | p1={pt['p_one_sided'] if pt else None} "
                  f"{'SIGNIF_PER_WINDOW_BONFERRONI' if sig else ''}", flush=True)
        result[wname] = wres

    # Бонферрони для July (4 теста)
    n_tests = 4
    alpha = 0.05 / n_tests
    july = result["July_2026"]
    bonf = {fname: (july[fname]["perm"]["p_one_sided"] < alpha if july[fname]["perm"] else None)
            for fname in july}
    result["bonferroni"] = {"n_tests": n_tests, "alpha": round(alpha, 4), "july_significant": bonf}
    print(f"\nBonferroni alpha={alpha}; July significant: {bonf}", flush=True)

    result["bonferroni"]["family"] = "four prespecified factors per window; no cross-window selection correction"
    result["bonferroni"]["by_window"] = {
        window: {name: (bool(values["perm"]["p_one_sided"] < alpha) if values["perm"] else None)
                 for name, values in result[window].items()}
        for window in windows
    }
    if file_hashes(inputs) != input_hashes:
        raise RuntimeError("input files changed during computation")
    if args.source == "db":
        for name, expected in factor_hashes.items():
            if factor_fingerprint(name, load_full_db(name)) != expected:
                raise RuntimeError(f"factor series changed during computation: {name}")
    destination = save_results(result, args.output_dir)
    print(f"saved reports to {destination}", flush=True)


if __name__ == "__main__":
    main()
