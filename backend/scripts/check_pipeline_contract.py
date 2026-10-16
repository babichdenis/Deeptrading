#!/usr/bin/env python3
"""Единый gate контракта пайплайна (B3).

Запускает явный набор новых регрессионных тестов и необходимых существующих
golden-тестов в изолированной тестовой PostgreSQL 16 и возвращает:

  0 — всё прошло (нет skip/xfail/ошибок, коллекция непустая);
  1 — есть падения;
  2 — отсутствует/непригоден обязательный PostgreSQL-контур;
  3 — нулевая коллекция;
  4 — обязательный кейс был пропущен (skip/xfail) — это НЕ зелёный результат.

Охват (системная регрессия источника→БД→CandleHub→исполнение):
  - test_pipeline_contract_e2e.py        — сквозной контур B3;
- test_intraday_trend_integration.py   — независимая интеграционная приёмка C3 (B3);
  - test_ingestion_contract.py           — контракт границы записи B1;
  - test_ingestion_postgres.py           — реальные транзакции записи B1 в тестовой БД;
  - test_research_execution_contract.py  — контракт исполнения A7/A24 (B2);
  - test_engine_golden/candlehub/candle_integrity — существующие golden.

Не использует торговый API и сеть, не исправляет данные автоматически.

Локальный запуск (без секретов):
    createdb -h 127.0.0.1 -U deeptrading deeptrading_test
    export PIPELINE_TEST_DATABASE_URL="postgresql+asyncpg://deeptrading:deeptrading@127.0.0.1:5432/deeptrading_test"
    python backend/scripts/check_pipeline_contract.py

Подробности — docs/PIPELINE_TESTING.md.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ENV_VAR = "PIPELINE_TEST_DATABASE_URL"

NEW_REGRESSION_TESTS = [
    "tests/test_pipeline_contract_e2e.py",
    "tests/test_intraday_trend_integration.py",
]
PIPELINE_PEER_CONTRACT_TESTS = [
    "tests/test_ingestion_contract.py",
    "tests/test_ingestion_postgres.py",
    "tests/test_research_execution_contract.py",
]
EXISTING_GOLDEN_TESTS = [
    "tests/test_engine_golden.py",
    "tests/test_candlehub.py",
    "tests/test_candle_integrity.py",
]
TARGETS = NEW_REGRESSION_TESTS + PIPELINE_PEER_CONTRACT_TESTS + EXISTING_GOLDEN_TESTS

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NO_CONTOUR = 2
EXIT_EMPTY = 3
EXIT_SKIPPED = 4


def _check_contour() -> int:
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    try:
        from tests.fixtures.pipeline_contract.db import (  # noqa: PLC0415
            PipelineContourError,
            resolve_test_target,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[gate] не удалось импортировать контур БД: {exc}")
        return EXIT_NO_CONTOUR
    raw = (os.environ.get(ENV_VAR) or "").strip()
    if not raw:
        print(f"[gate] {ENV_VAR} не задан: обязательный PostgreSQL-контур отсутствует.")
        print("       см. docs/PIPELINE_TESTING.md")
        return EXIT_NO_CONTOUR
    try:
        target = resolve_test_target()
    except PipelineContourError as exc:
        print(f"[gate] непригодный тестовый контур: {exc}")
        return EXIT_NO_CONTOUR
    print(f"[gate] тестовая БД: database={target.database!r} host={target.host!r} port={target.port!r}")
    return EXIT_OK


def _run_pytest() -> tuple[int, str]:
    env = dict(os.environ)
    env["PIPELINE_CONTRACT"] = "1"
    cmd = [
        sys.executable, "-m", "pytest", "-q", "--tb=short",
        "-p", "no:cacheprovider", "-o", "addopts=", *TARGETS,
    ]
    print(f"[gate] {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=str(BACKEND), env=env,
                          capture_output=True, text=True)  # noqa: S603
    return proc.returncode, proc.stdout + proc.stderr


def _skipped_or_xfail(out: str) -> bool:
    return bool(re.search(r"\d+ (?:skipped|xfailed|xpassed)\b", out))


def _empty_collection(out: str) -> bool:
    return bool(re.search(r"no tests ran", out)) or bool(re.search(r"collected 0 items", out))


def _errors(out: str) -> int:
    m = re.search(r"(\d+) errors?\b", out)
    return int(m.group(1)) if m else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return EXIT_OK

    contour = _check_contour()
    if contour != EXIT_OK:
        return contour

    code, out = _run_pytest()
    print(out)

    if _empty_collection(out):
        print("[gate] FAIL: нулевая коллекция тестов.")
        return EXIT_EMPTY
    if _skipped_or_xfail(out):
        print("[gate] FAIL: обязательный кейс пропущен (skip/xfail) — не зелёный результат.")
        return EXIT_SKIPPED
    if code != 0 or _errors(out):
        print("[gate] FAIL: есть падения/ошибки.")
        return EXIT_FAILED
    print("[gate] OK: контракт пайплайна выполнен.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
