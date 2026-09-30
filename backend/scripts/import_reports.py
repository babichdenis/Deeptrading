#!/usr/bin/env python3
"""Импорт отчётов из reports/ в таблицы report_runs/report_rows/report_trades/report_slices.

Идемпотентность — по content-hash файла: повторный запуск ничего не дублирует
(файл с тем же содержимым пропускается; --force перезаписывает).

Что импортируется (по умолчанию — только распознаваемые форматы):
  reports/bt_ose_real_*.json   kind=real   строки (робот×выход×тикер) + сделки;
  reports/ose_matrix_*.json    kind=matrix строки из raw;
  reports/experiments/WF_*.json kind=wf    агрегат по фазам OOS;
  reports/experiments/EXP-*.json kind=exp  строки + сделки из artifacts.per_ticker.

Срезы (сессия/режим ADX/ER/час/день/тикер) считаются в app/services/report_slices.py
и пишутся в report_slices — их же отдаёт API /api/v1/analysis/reports/{id}/slices.

Обогащение сделок (режим ADX, ER входа, SL/TP из EXITS-политик, MAE/MFE в ATR)
идёт из свечей БД; при недоступности свечей сделки пишутся «голыми», а режим/ER
получают бакет «нет данных». Отключается --no-enrich.

Запуск (из backend/):
  ~/.venvs/deeptrading/bin/python scripts/import_reports.py                # всё по умолчанию
  ~/.venvs/deeptrading/bin/python scripts/import_reports.py reports/bt_ose_real_20260930_0145.json
  ~/.venvs/deeptrading/bin/python scripts/import_reports.py --dry-run --no-enrich
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import get_settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.models.reports import ReportRow, ReportRun, ReportSlice, ReportTrade  # noqa: E402
from app.services.report_slices import DIMS, compute_slices, enrich_session  # noqa: E402
from sqlalchemy import create_engine, delete, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

DEFAULT_GLOBS = (
    "bt_ose_real_*.json",
    "bt_ose_matrix_*.json",
    "ose_matrix_*.json",
    "experiments/WF_*.json",
    "experiments/EXP-*.json",
)

TF_SECONDS = {
    "1min": 60, "1m": 60, "5min": 300, "10min": 600, "15min": 900,
    "30min": 1800, "1h": 3600, "hour": 3600,
}

ADX_PERIOD = 14
ER_LENGTH = 10

# Синхронный psycopg2 (libpq) по умолчанию шлёт GSSENCRequest; сервер с включённым
# GSSAPI отвечает 'G' и соединение виснет без таймаута. asyncpg этого не делает,
# поэтому сетевой бот GSS не видит — а разница тут только в драйвере.
_SYNC_CONNECT_ARGS = {"gssencmode": "disable"}


# --------------------------------------------------------------------------- utils

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _i(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _pf(gw: float, gl: float) -> float | None:
    if gl > 0:
        return round(gw / gl, 4)
    return 999.0 if gw > 0 else None


def detect_kind(path: Path, data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    if "experiment_id" in data:
        return "exp"
    if "phases" in data:
        return "wf"
    if "raw" in data or "results" in data:
        return "matrix" if "matrix" in path.name else "real"
    return None


def collect_files(root: Path, extra: Iterable[str]) -> list[Path]:
    seen: dict[Path, None] = {}
    for pattern in DEFAULT_GLOBS:
        for p in sorted(root.glob(pattern)):
            seen.setdefault(p.resolve(), None)
    for raw in extra:
        p = Path(raw)
        if p.is_dir():
            for sub in DEFAULT_GLOBS:
                for q in sorted(p.glob(sub)):
                    seen.setdefault(q.resolve(), None)
        elif p.is_file():
            seen.setdefault(p.resolve(), None)
    return list(seen)


# ------------------------------------------------------------------- kind: real / matrix

def _row_metrics(r: dict) -> dict:
    gw, gl = _f(r.get("gw")), _f(r.get("gl"))
    return {
        "strategy": str(r.get("strategy") or r.get("sid") or ""),
        "exit": str(r.get("exit") or ""),
        "ticker": str(r.get("ticker") or ""),
        "trades": _i(r.get("trades")),
        "wins": _i(r.get("wins")),
        "gw": gw,
        "gl": gl,
        "net": _f(r.get("net")),
        "pf": _pf(gw, gl),
        "max_dd_pct": None if r.get("max_dd_pct") is None else _f(r.get("max_dd_pct")),
        "commission": None if r.get("commission") is None else _f(r.get("commission")),
        "sec": None if r.get("sec") is None else _f(r.get("sec")),
        "raw": {
            k: r.get(k)
            for k in ("bars", "slippage", "turnover", "data_hash", "error", "exit_desc")
            if r.get(k) is not None
        } or None,
    }


def _trades_from_rows(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in rows:
        base = {
            "strategy": str(r.get("strategy") or r.get("sid") or ""),
            "exit": str(r.get("exit") or ""),
            "ticker": str(r.get("ticker") or ""),
        }
        for t in r.get("trades_detail") or []:
            out.append({
                **base,
                "side": str(t.get("side") or "LONG").upper(),
                "entry_time": _dt(t.get("entry_time")),
                "exit_time": _dt(t.get("exit_time")),
                "entry_price": t.get("entry_price"),
                "exit_price": t.get("exit_price"),
                "exit_reason": t.get("exit_reason"),
                "bars_held": None if t.get("bars_held") is None else _i(t.get("bars_held")),
                "net_pnl": _f(t.get("net_pnl")),
            })
    return out


TOPLEVEL_META_KEYS = (
    "period", "interval", "tickers", "qty", "commission", "slippage_bps",
    "quorum", "jobs", "duration_sec", "script", "created_utc", "name",
)


def parse_real(path: Path, data: dict) -> dict:
    meta = dict(data.get("meta") or {})
    if not meta:
        meta = {k: data.get(k) for k in TOPLEVEL_META_KEYS if data.get(k) is not None}
    src = [r for r in (data.get("raw") or []) if isinstance(r, dict)]
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        mtime = None
    return {
        "kind": "matrix" if "matrix" in path.name else "real",
        "name": str(meta.get("name") or path.stem),
        "created_at": _dt(meta.get("created_utc")) or mtime,
        "meta": {**meta, "aggregate": data.get("aggregate")},
        "rows": [_row_metrics(r) for r in src],
        "trades": _trades_from_rows(src),
    }


# ------------------------------------------------------------------------ kind: wf

def parse_wf(path: Path, data: dict) -> dict:
    meta = dict(data.get("meta") or {})
    agg: dict[str, dict] = {}
    for phase in data.get("phases") or []:
        if str(phase.get("type") or "").upper() != "OOS":
            continue
        for label, m in (phase.get("by_label") or {}).items():
            a = agg.setdefault(label, {
                "strategy": label, "exit": "", "ticker": "",
                "trades": 0, "wins": 0, "gw": 0.0, "gl": 0.0, "net": 0.0,
                "max_dd_pct": 0.0, "commission": 0.0, "sec": None,
                "raw": {"oos_phases": 0, "period": meta.get("period")},
            })
            a["trades"] += _i(m.get("trades"))
            a["wins"] += _i(m.get("wins"))
            a["gw"] += _f(m.get("gross_win"))
            a["gl"] += _f(m.get("gross_loss"))
            a["net"] += _f(m.get("net_pnl"))
            a["commission"] += _f(m.get("commission"))
            a["max_dd_pct"] = max(a["max_dd_pct"] or 0.0, _f(m.get("max_drawdown_pct")))
            a["raw"]["oos_phases"] += 1
    rows = []
    for a in agg.values():
        a["pf"] = _pf(a["gw"], a["gl"])
        rows.append(a)
    return {
        "kind": "wf",
        "name": str(meta.get("name") or path.stem),
        "created_at": _dt(meta.get("created_utc")),
        "meta": meta,
        "rows": rows,
        "trades": [],
    }


# ------------------------------------------------------------------------ kind: exp

def parse_exp(path: Path, data: dict) -> dict:
    config = data.get("config") or {}
    meta = {
        "experiment_id": data.get("experiment_id"),
        "config": config,
        "metrics": data.get("metrics"),
        "filters": data.get("filters"),
        "code_sha": data.get("code_sha"),
        "data_hash": data.get("data_hash"),
        "source_spec": data.get("source_spec"),
        "period": config.get("period"),
        "interval": config.get("timeframe"),
    }
    per_ticker = ((data.get("artifacts") or {}).get("per_ticker")) or {}
    rows: list[dict] = []
    trades: list[dict] = []
    label = str(data.get("name") or path.stem)
    exit_code = ""
    if isinstance(config.get("exits"), list) and config["exits"]:
        exit_code = str(config["exits"][0])
    for ticker, blob in sorted(per_ticker.items()):
        tlist = blob.get("trades") or []
        nets = [_f(t.get("net_pnl")) for t in tlist]
        gw = sum(x for x in nets if x > 0)
        gl = -sum(x for x in nets if x <= 0)
        rows.append({
            "strategy": label, "exit": exit_code, "ticker": ticker,
            "trades": len(nets), "wins": sum(1 for x in nets if x > 0),
            "gw": round(gw, 6), "gl": round(gl, 6), "net": round(sum(nets), 6),
            "pf": _pf(gw, gl), "max_dd_pct": None,
            "commission": None, "sec": None,
            "raw": {"data_hash": blob.get("data_hash")},
        })
        for t in tlist:
            trades.append({
                "strategy": label, "exit": exit_code, "ticker": ticker,
                "side": str(t.get("side") or "LONG").upper(),
                "entry_time": _dt(t.get("entry_time")),
                "exit_time": _dt(t.get("exit_time")),
                "entry_price": t.get("entry_price"),
                "exit_price": t.get("exit_price"),
                "exit_reason": t.get("exit_reason"),
                "bars_held": None if t.get("bars_held") is None else _i(t.get("bars_held")),
                "net_pnl": _f(t.get("net_pnl")),
            })
    return {
        "kind": "exp",
        "name": label,
        "created_at": _dt(data.get("created_utc")),
        "meta": meta,
        "rows": rows,
        "trades": trades,
    }


PARSERS = {"real": parse_real, "matrix": parse_real, "wf": parse_wf, "exp": parse_exp}


# ------------------------------------------------------------------- enrichment

def _group_by_ticker(trades: list[dict]) -> dict[str, list[dict]]:
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        enrich_session(t)
        by_ticker[str(t["ticker"])].append(t)
    return by_ticker


def _load_series(engine, names: list[str], meta: dict) -> dict[str, list]:
    """Свечи периода на ТФ прогона (1m из БД → build_tf) по каждому тикеру."""
    from app.engine.candlehub import build_tf
    from app.engine.models import Candle
    from sqlalchemy import text

    period = [_dt(meta["period"][0]), _dt(meta["period"][-1])] if meta.get("period") else []
    tf_sec = TF_SECONDS.get(str(meta.get("interval") or "1h"))
    if tf_sec is None or len(period) != 2 or period[0] is None or period[1] is None:
        return {}
    t0 = (period[0] - timedelta(days=3)).date().isoformat()
    t1 = f"{period[1].date().isoformat()} 23:59:59+00"

    out: dict[str, list] = {}
    with engine.connect() as conn:
        fmap = {
            str(r[1]): str(r[0])
            for r in conn.execute(
                text("SELECT figi, upper(ticker) FROM instruments WHERE upper(ticker) = ANY(:t)"),
                {"t": names},
            ).fetchall()
        }
        for tk in names:
            figi = fmap.get(tk)
            if not figi:
                continue
            raws = conn.execute(
                text(
                    "SELECT ts, open, high, low, close, volume FROM candles "
                    "WHERE figi=:f AND interval=1 AND ts>=:t0 AND ts<=:t1 ORDER BY ts"
                ),
                {"f": figi, "t0": f"{t0} 00:00+00", "t1": t1},
            ).fetchall()
            if not raws:
                continue
            bars = [
                Candle(ts=r[0], open=float(r[1]), high=float(r[2]), low=float(r[3]),
                       close=float(r[4]), volume=float(r[5] or 0))
                for r in raws
            ]
            series = bars if tf_sec == 60 else build_tf(bars, tf_sec)
            if series:
                out[tk] = series
    return out


def enrich_from_series(by_ticker: dict[str, list[dict]],
                       series_by_ticker: dict[str, list]) -> tuple[int, int]:
    """Чистая часть обогащения — данные уже в памяти (её же гоняют тесты).

    Заполняет regime_adx / er_in / sl_price / tp_price / mae_atr / mfe_atr.
    Возвращает (сколько тикеров обогащено, сколько сделок).
    """
    if not by_ticker or not series_by_ticker:
        return 0, 0
    from app.engine.indicatorhub import _adx, _atr, _efficiency_ratio
    from app.engine.models import Side
    from app.services.report_slices import adx_regime_bucket

    from ose_exit_matrix import EXITS

    done_tickers = 0
    done_trades = 0
    for tk, series in series_by_ticker.items():
        trades = by_ticker.get(tk)
        if not trades or not series:
            continue
        ts_list = [b.ts for b in series]
        adx_s = _adx(series, ADX_PERIOD)["adx"]
        er_s = _efficiency_ratio(series, ER_LENGTH)
        atr_s = _atr(series, ADX_PERIOD)
        n = len(series)
        exit_cache: dict[str, Any] = {}
        for t in trades:
            enrich_session(t)
            entry_ts = t.get("entry_time")
            if entry_ts is None:
                continue
            idx = bisect.bisect_right(ts_list, entry_ts) - 1
            if idx < 0 or idx >= n:
                continue
            ax = adx_s[idx] if idx < len(adx_s) else None
            er = er_s[idx] if idx < len(er_s) else None
            t["regime_adx"] = adx_regime_bucket(float(ax) if ax is not None else None)
            t["er_in"] = None if er is None else round(float(er), 4)

            code = str(t.get("exit") or "")
            if code and code not in exit_cache:
                try:
                    exit_cache[code] = EXITS[code][1]()
                except Exception:
                    exit_cache[code] = None
            policy = exit_cache.get(code)
            if policy is not None and t.get("entry_price") is not None:
                try:
                    side = Side.BUY if t.get("side") == "LONG" else Side.SELL
                    plan = policy.plan_entry(side, float(t["entry_price"]), series[: idx + 1])
                    t["sl_price"] = plan.stop_loss
                    t["tp_price"] = plan.take_profit
                except Exception:
                    pass

            exit_ts = t.get("exit_time")
            j = bisect.bisect_left(ts_list, exit_ts) if exit_ts is not None else idx
            j = max(idx, min(j, n - 1))
            window = series[idx: j + 1]
            atr_v = atr_s[idx] if idx < len(atr_s) else None
            if window and atr_v and atr_v > 0:
                lo = min(b.low for b in window)
                hi = max(b.high for b in window)
                ep = float(t["entry_price"])
                if t.get("side") == "SHORT":
                    mae, mfe = hi - ep, ep - lo
                else:
                    mae, mfe = ep - lo, hi - ep
                t["mae_atr"] = round(mae / atr_v, 3)
                t["mfe_atr"] = round(mfe / atr_v, 3)
            done_trades += 1
        done_tickers += 1
    return done_tickers, done_trades


def enrich_trades(trades: list[dict], meta: dict, engine) -> tuple[int, int]:
    """Загружает свечи из БД и обогащает сделки. Возвращает (тикеров, сделок)."""
    if not trades:
        return 0, 0
    by_ticker = _group_by_ticker(trades)
    series = _load_series(engine, sorted(by_ticker), meta)
    if not series:
        return 0, 0
    return enrich_from_series(by_ticker, series)


# ------------------------------------------------------------------------ write

def _display_name(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return path.name

def import_file(session: Session, path: Path, engine, args) -> str:
    raw_bytes = path.read_bytes()
    chash = hashlib.sha256(raw_bytes).hexdigest()
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        return f"SKIP  {path.name}: не парсится ({e})"

    kind = detect_kind(path, data)
    if kind is None or kind not in PARSERS:
        return f"SKIP  {path.name}: нераспознанный формат"

    existing = session.execute(
        select(ReportRun).where(ReportRun.content_hash == chash)
    ).scalar_one_or_none()
    if existing is not None and not args.force:
        return f"OK    {path.name}: уже импортирован (run #{existing.id}, без изменений)"
    if existing is not None and args.force:
        # Явно снимаем детей: каскад через FK работает не в каждой СУБД
        # (SQLite держит PRAGMA foreign_keys=OFF по умолчанию).
        for model in (ReportSlice, ReportTrade, ReportRow):
            session.execute(delete(model).where(model.run_id == existing.id))
        session.execute(delete(ReportRun).where(ReportRun.id == existing.id))

    parsed = PARSERS[kind](path, data)
    mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)

    n_enriched_tk = n_enriched_tr = 0
    if parsed["trades"] and not args.no_enrich and engine is not None:
        try:
            n_enriched_tk, n_enriched_tr = enrich_trades(parsed["trades"], parsed["meta"], engine)
        except Exception as e:  # noqa: BLE001
            print(f"      ! обогащение не удалось: {type(e).__name__}: {str(e)[:160]}")

    if args.dry_run:
        return (f"DRY   {path.name}: kind={parsed['kind']} "
                f"rows={len(parsed['rows'])} trades={len(parsed['trades'])} "
                f"hash={chash[:12]}")

    run = ReportRun(
        file_name=_display_name(path, Path(args.reports_dir)),
        kind=parsed["kind"],
        name=parsed["name"],
        created_at=parsed["created_at"],
        meta=parsed["meta"],
        content_hash=chash,
        mtime=mtime,
    )
    session.add(run)
    session.flush()

    if parsed["rows"]:
        session.bulk_insert_mappings(
            ReportRow, [{**r, "run_id": run.id} for r in parsed["rows"]]
        )
    if parsed["trades"]:
        cols = {c.name for c in ReportTrade.__table__.columns} - {"id"}
        session.bulk_insert_mappings(
            ReportTrade,
            [{k: v for k, v in t.items() if k in cols} | {"run_id": run.id}
             for t in parsed["trades"]],
        )

    for t in parsed["trades"]:
        t.setdefault("strategy", "")
        if not t.get("session"):
            enrich_session(t)
    slices = compute_slices(parsed["trades"], dims=DIMS, strategy_key="strategy")
    if slices:
        session.bulk_insert_mappings(ReportSlice, [{**s, "run_id": run.id} for s in slices])

    session.commit()
    extra = ""
    if parsed["trades"]:
        extra = f" trades={len(parsed['trades'])} (обогащено {n_enriched_tr}/{len(parsed['trades'])} по {n_enriched_tk} тикерам)"
    return (f"NEW   {path.name}: kind={parsed['kind']} run=#{run.id} "
            f"rows={len(parsed['rows'])} slices={len(slices)}{extra} hash={chash[:12]}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("paths", nargs="*", help="файлы/папки (по умолчанию — известные шаблоны в reports/)")
    p.add_argument("--reports-dir", default=str(Path(__file__).resolve().parents[1] / "reports"))
    p.add_argument("--engine-url", default=None, help="DSN (по умолчанию из настроек)")
    p.add_argument("--no-enrich", action="store_true", help="не считать ADX/ER/SL/TP/MAE/MFE из свечей")
    p.add_argument("--force", action="store_true", help="перезаписать прогоны с тем же хэшем")
    p.add_argument("--dry-run", action="store_true", help="ничего не писать в БД")
    a = p.parse_args(argv)

    files = collect_files(Path(a.reports_dir), a.paths)
    if not files:
        print("нет файлов для импорта")
        return 1

    url = (a.engine_url or get_settings().database_url).replace("+asyncpg", "")
    engine = None
    session: Session | None = None
    if not a.dry_run:
        engine = create_engine(url, pool_pre_ping=True, connect_args=_SYNC_CONNECT_ARGS)
        Base.metadata.create_all(engine)
        session = Session(engine)

    t0 = time.time()
    new = skipped = 0
    try:
        for f in files:
            if session is None:
                data = json.loads(f.read_text(encoding="utf-8"))
                kind = detect_kind(f, data)
                parsed = PARSERS[kind](f, data) if kind in PARSERS else None
                if parsed is None:
                    print(f"SKIP  {f.name}: нераспознанный формат")
                    continue
                print(f"DRY   {f.name}: kind={parsed['kind']} rows={len(parsed['rows'])} "
                      f"trades={len(parsed['trades'])} hash={_sha256(f)[:12]}")
                new += 1
                continue
            msg = import_file(session, f, engine, a)
            print(msg)
            if msg.startswith("NEW") or msg.startswith("DRY"):
                new += 1
            else:
                skipped += 1
    finally:
        if session is not None:
            session.close()
        if engine is not None:
            engine.dispose()

    print(f"\nитог: файлов {len(files)}, новых {new}, пропущено {skipped}, {time.time() - t0:.1f}с")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
