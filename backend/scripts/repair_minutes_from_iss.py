#!/usr/bin/env python3
"""Ремонт минутного ряда из MOEX ISS: перекачка за диапазон дат с проверками.

Зачем: история до 24.11.2025 по 26 бумагам записана в других единицах (цена меньше
биржевой примерно в 4.27 раза, объём больше примерно в 200 раз) и с перепутанными
low/close. Перестановкой полей это не чинится — участок надо перекачать.

Инструмент постоянный: принимает любые годы, бумаги и интервал, пишет через контракт
приёма данных, отвергнутые строки не прячет, а считает и логирует, и сверяет результат
с независимым дневным рядом из той же базы.

Запуск:
  python scripts/repair_minutes_from_iss.py --from 2026-01-01 --to 2026-10-09 --dry
  python scripts/repair_minutes_from_iss.py --from 2026-01-01 --to 2026-10-09 --write
  python scripts/repair_minutes_from_iss.py --tickers SBER,GAZP --from ... --to ... --write
  python scripts/repair_minutes_from_iss.py --verify --from ... --to ...
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services.candle_ingestion_contract import assert_valid_batch  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MSK = ZoneInfo("Europe/Moscow")
ISS = "https://iss.moex.com/iss/engines/stock/markets/{m}/securities/{t}/candles.json"
# Индексы живут в отдельном рынке биржи и в справочнике instruments их нет.
INDEX_FIGI = {"IMOEX": "BBG00KDWPPW2"}
MANIFEST = Path(__file__).resolve().parents[1] / "configs" / "data_invalidity.json"
BAD = "high < low OR high < GREATEST(open,close) OR low > LEAST(open,close)"


def _ssl_context():
    """Явный список корневых сертификатов: иначе при частых запросах проверка рвётся."""
    import ssl

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


_CTX = _ssl_context()


def fetch(url: str, tries: int = 8) -> dict:
    last: Exception | None = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=30, context=_CTX) as resp:
                return json.loads(resp.read())
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 + 1.5 * i)
    raise RuntimeError(f"ISS недоступен: {last}")


def iss_day(ticker: str, day: date, interval: int, market: str = "shares") -> list[dict]:
    out: list[dict] = []
    start = 0
    while True:
        url = ISS.format(m=market, t=ticker) + f"?from={day}&till={day}&interval={interval}&start={start}"
        data = fetch(url)
        cols = {n: i for i, n in enumerate(data["candles"]["columns"])}
        rows = data["candles"]["data"]
        for r in rows:
            ts = datetime.fromisoformat(r[cols["begin"]]).replace(tzinfo=MSK).astimezone(timezone.utc)
            out.append({
                "ts": ts,
                "open": float(r[cols["open"]]),
                "high": float(r[cols["high"]]),
                "low": float(r[cols["low"]]),
                "close": float(r[cols["close"]]),
                "volume": float(r[cols["volume"]]),
            })
        if len(rows) < 500:
            break
        start += len(rows)
    return out


def engine():
    return create_engine(get_settings().database_url.replace("+asyncpg", ""), pool_pre_ping=True)


def target_figis(eng, tickers: list[str], market: str = "shares") -> list[tuple[str, str]]:
    """(figi, ticker) из манифеста невалидности, суженные до перечисленных тикеров."""
    if market == "index":
        if tickers:
            return [(INDEX_FIGI[t.upper()], t.upper()) for t in tickers
                    if t.upper() in INDEX_FIGI]
        return [(figi, t) for t, figi in INDEX_FIGI.items()]
    with eng.connect() as c:
        if tickers:
            rows = c.execute(text(
                "SELECT figi, ticker FROM instruments WHERE upper(ticker) = ANY(:t)"),
                {"t": [t.upper() for t in tickers]}).all()
        else:
            blob = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {"ranges": []}
            figis = sorted({r["figi"] for r in blob.get("ranges", [])})
            rows = c.execute(text(
                "SELECT figi, ticker FROM instruments WHERE figi = ANY(:f)"), {"f": figis}).all()
    return [(r[0], r[1]) for r in rows]


def upsert(eng, figi: str, rows: list[dict], day: date | None = None) -> tuple[int, int]:
    """Заменить день целиком: удалить прежние минуты дня и вписать пришедшие с биржи.

    Удаление обязательно: старый ряд мог лежать в других единицах на других минутах,
    и одна вставка оставит его рядом, не исправив день.
    Возвращает (вставлено, удалено).
    """
    if not rows:
        return 0, 0
    clean = assert_valid_batch(
        [{"figi": figi, "interval": 1, "ts": r["ts"], "open": r["open"], "high": r["high"],
          "low": r["low"], "close": r["close"], "volume": r["volume"]} for r in rows])
    q = text(
        "INSERT INTO candles (figi, interval, ts, open, high, low, close, volume) "
        "VALUES (:figi, 1, :ts, :open, :high, :low, :close, :volume) "
        "ON CONFLICT (figi, interval, ts) DO UPDATE SET open=EXCLUDED.open, high=EXCLUDED.high, "
        "low=EXCLUDED.low, close=EXCLUDED.close, volume=EXCLUDED.volume")
    params = [{"figi": figi, "ts": r["ts"], "open": r["open"], "high": r["high"],
               "low": r["low"], "close": r["close"], "volume": int(r["volume"])} for r in clean]
    removed = 0
    with eng.begin() as db:
        if day is not None:
            start = datetime.combine(day, datetime.min.time(), tzinfo=MSK).astimezone(timezone.utc)
            end = start + timedelta(days=1)
            removed = db.execute(text(
                "DELETE FROM candles WHERE figi=:f AND interval=1 AND ts >= :a AND ts < :b"),
                {"f": figi, "a": start, "b": end}).rowcount or 0
        db.execute(q, params)
    return len(params), removed


def purge_bad_day(eng, figi: str, day: date) -> int:
    """Убрать за день только строки, нарушающие вложение OHLC.

    Когда биржа за день ничего не отдала (праздник, выходной), прежние строки этого
    дня остаются нетронутыми. Здесь снимаются лишь те из них, что заведомо испорчены;
    валидные строки сохраняются.
    """
    start = datetime.combine(day, datetime.min.time(), tzinfo=MSK).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    with eng.begin() as db:
        n = db.execute(text(
            f"DELETE FROM candles WHERE figi=:f AND interval=1 AND ts >= :a AND ts < :b AND ({BAD})"),
            {"f": figi, "a": start, "b": end}).rowcount or 0
    return n


def verify_day(eng, figi: str, day: date) -> tuple[bool, str]:
    """Сверка минутного дня с независимым дневным рядом из той же базы."""
    with eng.connect() as c:
        day_row = c.execute(text(
            "SELECT open, high, low, close FROM candles WHERE figi=:f AND interval=24 AND ts::date = :d"),
            {"f": figi, "d": day}).one_or_none()
        mins = c.execute(text(
            "SELECT open, high, low, close FROM candles WHERE figi=:f AND interval=1 AND ts::date = :d ORDER BY ts"),
            {"f": figi, "d": day}).all()
    if day_row is None:
        return True, "нет дневного эталона — пропуск сверки"
    if not mins:
        return False, "минут за день нет"
    o = float(mins[0][0])
    h = max(float(m[1]) for m in mins)
    lo = min(float(m[2]) for m in mins)
    cl = float(mins[-1][3])
    tol = 0.01 * max(1.0, abs(float(day_row[3])))
    checks = {
        "open": abs(o - float(day_row[0])) <= tol,
        "high": abs(h - float(day_row[1])) <= max(tol, 0.005 * float(day_row[1])),
        "low": abs(lo - float(day_row[2])) <= max(tol, 0.005 * float(day_row[2])),
        "close": abs(cl - float(day_row[3])) <= tol,
    }
    bad = [k for k, v in checks.items() if not v]
    if bad:
        return False, (f"расходится с дневным: {','.join(bad)} "
                       f"(минуты {o:.2f}/{h:.2f}/{lo:.2f}/{cl:.2f} vs день "
                       f"{float(day_row[0]):.2f}/{float(day_row[1]):.2f}/"
                       f"{float(day_row[2]):.2f}/{float(day_row[3]):.2f})")
    return True, "сходится с дневным"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="dfrom", required=True, help="начало YYYY-MM-DD")
    ap.add_argument("--to", dest="dto", required=True, help="конец YYYY-MM-DD")
    ap.add_argument("--tickers", default="", help="список через запятую (пусто = все из манифеста)")
    ap.add_argument("--market", default="shares", choices=["shares", "index"],
                    help="shares = акции TQBR (по умолчанию), index = индексы (IMOEX)")
    ap.add_argument("--interval", type=int, choices=(1,), default=1)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry", action="store_true")
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--verify", action="store_true", help="только сверка с дневным рядом")
    ap.add_argument("--purge-bad-empty", action="store_true",
                    help="для дней без данных биржи убрать лишь строки, нарушающие OHLC")
    ap.add_argument("--log", default="", help="файл отчёта JSON")
    a = ap.parse_args()

    dfrom = date.fromisoformat(a.dfrom)
    dto = date.fromisoformat(a.dto)
    if dto < dfrom:
        print("конец раньше начала")
        return 2
    eng = engine()
    tickers = [t.strip() for t in a.tickers.split(",") if t.strip()]
    targets = target_figis(eng, tickers, a.market)
    if not targets:
        print("не найдено бумаг для ремонта")
        return 2

    days = []
    cur = dfrom
    while cur <= dto:
        if cur.weekday() < 7:
            days.append(cur)
        cur += timedelta(days=1)

    print(f"бумаг {len(targets)}, дней {len(days)} ({dfrom}..{dto}), интервал {a.interval}")
    report = {"from": a.dfrom, "to": a.dto, "figis": [], "written": 0, "removed": 0,
              "purged_bad": 0, "rejected": 0, "verify_failed": [], "empty_days": 0}

    for figi, ticker in targets:
        written = rejected = empty = removed = purged = 0
        for day in days:
            try:
                rows = iss_day(ticker, day, a.interval, a.market)
            except Exception as e:  # noqa: BLE001
                rejected += 1
                print(f"  {ticker} {day}: биржа не отдала ({str(e)[:120]})")
                continue
            if not rows:
                empty += 1
                if a.purge_bad_empty and a.write:
                    purged += purge_bad_day(eng, figi, day)
                continue
            if a.verify:
                ok, why = verify_day(eng, figi, day)
                if not ok:
                    report["verify_failed"].append(f"{ticker} {day}: {why}")
                continue
            if not a.write:
                written += len(rows)
                continue
            try:
                n, rem = upsert(eng, figi, rows, day)
                written += n
                removed += rem
            except Exception as e:  # noqa: BLE001
                rejected += 1
                print(f"  {ticker} {day}: отвергнуто ({str(e)[:120]})")
        report["figis"].append({"ticker": ticker, "written": written, "removed": removed,
                                "purged_bad": purged, "rejected_days": rejected, "empty_days": empty})
        report["written"] += written
        report["removed"] += removed
        report["purged_bad"] += purged
        report["rejected"] += rejected
        report["empty_days"] += empty
        print(f"{ticker}: баров {written}, заменено старых {removed}, "
              f"снято битых в пустые дни {purged}, "
              f"отвергнутых дней {rejected}, пустых дней {empty}")

    print(f"итого баров {report['written']}, заменено старых {report['removed']}, "
          f"снято битых в пустые дни {report['purged_bad']}, "
          f"отвергнутых дней {report['rejected']}, "
          f"пустых дней {report['empty_days']}, расхождений с дневным {len(report['verify_failed'])}")
    if report["verify_failed"]:
        for line in report["verify_failed"][:10]:
            print("  ", line)
    if a.log:
        Path(a.log).parent.mkdir(parents=True, exist_ok=True)
        Path(a.log).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"лог: {a.log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
