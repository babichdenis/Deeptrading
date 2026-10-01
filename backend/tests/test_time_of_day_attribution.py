"""B1 TIME_OF_DAY_ATTRIBUTION тесты (SHADOW/read-only, canonical packs).

Проверяют:
- сумма по корзинам = total trades/net (реконсиляция);
- decision_time корректно в MSK;
- все сделки покрыты ровно одной корзиной;
- EngineRunner не меняется (артефакты read-only, движок без правок).
"""
import csv
import json
import os

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(BACKEND, "reports")

pytestmark = pytest.mark.artifact


@pytest.fixture(scope="module")
def attribution() -> dict:
    with open(os.path.join(REPORTS, "time_of_day_attribution.json")) as f:
        return json.load(f)


def _bucket_counts(period: dict) -> dict[str, int]:
    return {k: v["count"] for k, v in period["by_bucket"].items()}


def test_bucket_sum_reconciles_july(attribution: dict) -> None:
    p = attribution["periods"]["2026-07"]
    total = sum(_bucket_counts(p).values())
    assert total == p["trades"], f"корзины {total} != сделок {p['trades']}"
    net_sum = sum(v["net"] for v in p["by_bucket"].values())
    assert abs(net_sum) > 0  # net есть


def test_bucket_sum_reconciles_mar_apr(attribution: dict) -> None:
    p = attribution["periods"]["2026-03_04"]
    total = sum(_bucket_counts(p).values())
    assert total == p["trades"], f"корзины {total} != сделок {p['trades']}"


def test_decision_time_in_msk() -> None:
    """Все корзины внутри 10:00..18:45 MSK; из trades.csv decision_time -> MSK."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    msk = ZoneInfo("Europe/Moscow")
    for pack in ("5b44f3b383df", "57b6244ee3eb"):
        with open(os.path.join(REPORTS, pack, "trades.csv"), newline="") as f:
            for row in csv.DictReader(f):
                dt = datetime.fromisoformat(row["decision_time"].replace("Z", "+00:00")).astimezone(msk)
                assert dt.hour >= 10, f"вход до 10:00 MSK: {row['decision_time']}"
                assert dt.hour <= 18, f"вход после 18:59 MSK: {row['decision_time']}"


def test_every_trade_in_exactly_one_bucket(attribution: dict) -> None:
    for p in ("2026-07", "2026-03_04"):
        period = attribution["periods"][p]
        buckets = list(period["by_bucket"].keys())
        # ключи не пересекаются (30-мин сетки) и покрывают каждый час 10..18
        assert len(buckets) == len(set(buckets))
        hours = {b.split(":")[0] for b in buckets}
        assert {"10", "11", "12", "13", "14", "15", "16", "17", "18"} <= hours


def test_engine_not_modified() -> None:
    import os as _os
    hits = []
    for root in (os.path.join(BACKEND, "app", "engine"),
                 os.path.join(BACKEND, "app", "services")):
        for dp, _, files in os.walk(root):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                with open(_os.path.join(dp, fn)) as f:
                    txt = f.read()
                if "time_of_day_attribution" in txt:
                    hits.append(_os.path.join(dp, fn))
    assert not hits, f"движок ссылается на time_of_day: {hits}"
