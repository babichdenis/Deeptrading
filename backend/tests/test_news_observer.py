"""Тесты ядра news_observer (A6). Полностью hermetic: сети нет.

Обновлены под r4 (REWORK 2026-10-09, единый остаток): идентичность версии по
исходному payload (П.1), строгая привязка тикера/evidence/source_version_ids (П.2),
coverage по обязательным источникам со свежестью успешного наблюдения и приоритет
подтверждённого veto (П.3), нормализация времени без мутации (П.4), отказ connect
на старой схеме (П.4). Файл — уже существовавшее (r2) отклонение от контракта; в r4
поддержан в согласованном состоянии.

R2 (2026-10-11): исправлена фикстура ветки C — незакрытое событие должно называть
кандидата (релевантное), а не другой тикер наблюдателя. GAZP входит в
OBSERVER_TICKERS, поэтому по контракту П.1 r5 «другой тикер наблюдателя → skip»:
событие про GAZP доказано нерелевантно SBER и не может дать
incomplete_analysis_coverage. Добавлено покрытие нерелевантного события (другой
тикер наблюдателя), неустановленной релевантности (без тикера) и временной
причинности (completed_at анализа гейтит его видимость точке решения).
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import news_observer_core as noc  # noqa: E402

RECEIVED = "2026-10-05T10:00:00Z"

RSS = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<rss version="2.0"><channel>'
    b'<item><title>Alpha first news</title><link>https://example.com/a1</link>'
    b'<pubDate>Mon, 05 Oct 2026 09:00:00 +0300</pubDate></item>'
    b'<item><title>Alpha second news</title><link>https://example.com/a2</link>'
    b'<pubDate>Mon, 05 Oct 2026 10:30:00 +0300</pubDate></item>'
    b'<item><title></title><link>https://example.com/a3</link></item>'
    b'</channel></rss>'
)

MOEX = json.dumps({
    "sitenews": {
        "columns": ["id", "title", "published_at"],
        "data": [[123, "MOEX news about listing", "2026-10-05 12:00:00"]],
    }
}).encode("utf-8")


@pytest.fixture()
def conn(tmp_path):
    c = noc.connect(tmp_path / "news.sqlite")
    yield c
    c.close()


def _insert_item(conn, title="Sanctions review announced for X"):
    return noc.upsert_item(conn, "RSS1", {"title": title, "url": "u9"},
                           RECEIVED)["item_id"]


def _analysis_obj(**over):
    obj = {
        "event_type": "sanctions",
        "entity": "Company X",
        "sector": "metals",
        "known_or_new_or_update": "new",
        "possible_impact": "negative",
        "horizon": "days",
        "uncertainty": "high",
        "tickers": ["X"],
        "evidence_spans": ["Sanctions review"],
        "reason": "заголовок прямо называет событие",
    }
    obj.update(over)
    return obj


FUTURE = "2100-01-01T00:00:00Z"


# --- константы и окружение -----------------------------------------------------------

def test_constants_contract():
    assert noc.FETCH_RETRIES == 0 and noc.DS_RETRIES == 0
    assert noc.DS_MAX_INPUT_CHARS == 8000 and noc.DS_MAX_OUTPUT_TOKENS == 1000
    assert noc.LATE_THRESHOLD_MIN == 60
    assert noc.PROMPT_VERSION == "a6-v2"
    assert noc.SCHEMA_VERSION == 3
    assert noc.EVENT_TYPES == ("sanctions", "accident", "corporate_action",
                               "earnings", "macro", "other", "unknown")
    assert noc.POSSIBLE_IMPACTS == ("positive", "negative", "mixed", "unknown")
    assert noc.HORIZONS == ("intraday", "days", "unknown")


def test_db_path_must_be_local():
    bad = r"\\server\share\news.sqlite" if sys.platform == "win32" else "/Volumes/Dev/news.sqlite"
    with pytest.raises(ValueError, match="ARCHIVE_PATH_NOT_LOCAL"):
        noc.ensure_local_db_path(bad)


def test_parse_utc_naive_not_utc():
    assert noc.parse_utc("2026-10-05T10:00:00Z") is not None
    assert noc.parse_utc("2026-10-05T10:00:00") is None  # без timezone — не UTC молча


# --- БД и источники ------------------------------------------------------------------

def test_connect_creates_schema(conn):
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"meta", "sources", "items", "item_versions", "analyses",
            "source_events", "candidates", "shadow_decisions",
            "morning_summaries", "labels"} <= tables
    row = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert row[0] == str(noc.SCHEMA_VERSION)


def test_sources_allowlist(conn):
    noc.ensure_source(conn, "RSS1", "rss", "https://example.com/rss")
    assert noc.source_allowed(conn, "RSS1") is True
    noc.add_source(conn, "RSS1", "rss", "https://example.com/rss", allowed=False)
    assert noc.source_allowed(conn, "RSS1") is False
    assert "RSS1" in {s["name"] for s in noc.list_sources(conn)}


# --- парсеры --------------------------------------------------------------------------

def test_detect_kind():
    assert noc.detect_kind(RSS) == "rss"
    assert noc.detect_kind(MOEX) == "moex"
    assert noc.detect_kind(b"garbage bytes") is None


def test_parse_rss_bytes():
    items = noc.parse_rss_bytes(RSS)
    assert len(items) == 2  # элемент с пустым заголовком пропущен
    assert items[0]["title"] == "Alpha first news"
    assert items[0]["raw_title"] == "Alpha first news"
    assert items[0]["url"] == "https://example.com/a1"
    assert items[0]["published_at"] == "2026-10-05T06:00:00Z"  # +0300 → UTC


def test_parse_moex_bytes():
    items = noc.parse_moex_bytes(MOEX)
    assert len(items) == 1
    assert items[0]["external_id"] == "123"
    assert items[0]["url"] == "https://www.moex.com/n123"
    assert items[0]["published_at"] == "2026-10-05T09:00:00Z"  # MSK → UTC


# --- upsert: дедуп, исправления, late-arrival, версии (П.1) --------------------------

def test_upsert_insert_duplicate_update(conn):
    item = {"title": "First news", "url": "https://example.com/n1",
            "published_at": "2026-10-05T09:00:00Z"}
    r1 = noc.upsert_item(conn, "RSS1", item, RECEIVED)
    assert r1["status"] == "inserted"
    assert r1["minutes_late"] == 60 and r1["late_arrival"] == 0
    r2 = noc.upsert_item(conn, "RSS1", item, RECEIVED)
    assert r2["status"] == "duplicate" and r2["item_id"] == r1["item_id"]
    r3 = noc.upsert_item(conn, "RSS1",
                         {"title": "First news (updated)",
                          "url": "https://example.com/n1",
                          "published_at": "2026-10-05T09:00:00Z"},
                         "2026-10-05T12:00:00Z")
    assert r3["status"] == "title_updated" and r3["item_id"] == r1["item_id"]
    row = conn.execute("SELECT title, corrected, url FROM items WHERE id=?",
                       (r1["item_id"],)).fetchone()
    assert row["title"] == "First news (updated)" and row["corrected"] == 1
    assert row["url"] == "https://example.com/n1"  # прежний url сохранён
    # append-only: две версии (inserted + title_updated), дубль версию не создаёт
    versions = conn.execute("SELECT version_id, normalized_text, last_seen_at "
                            "FROM item_versions WHERE source_item_id=? ORDER BY version_id",
                            (r1["item_id"],)).fetchall()
    assert len(versions) == 2
    assert versions[0]["normalized_text"] == "First news"
    assert versions[1]["normalized_text"] == "First news (updated)"


def test_versions_point_in_time_and_revert(conn):
    id0 = _insert_item(conn, title="A original")  # received 2026-10-05T10:00:00Z
    # повтор того же текста не меняет историю
    noc.upsert_item(conn, "RSS1", {"title": "A original", "url": "u9"},
                    "2026-10-05T11:00:00Z")
    noc.upsert_item(conn, "RSS1", {"title": "B corrected", "url": "u9"},
                    "2026-10-05T12:00:00Z")
    noc.upsert_item(conn, "RSS1", {"title": "A original", "url": "u9"},
                    "2026-10-05T13:00:00Z")  # B→A revert = НОВАЯ версия
    assert len(conn.execute("SELECT * FROM item_versions WHERE source_item_id=?",
                            (id0,)).fetchall()) == 3
    # до первой версии элемента как будто нет
    assert noc.visible_version(conn, id0, "2026-10-05T09:00:00Z") is None
    assert noc.visible_version(conn, id0, "2026-10-05T10:30:00Z")["normalized_text"] == "A original"
    assert noc.visible_version(conn, id0, "2026-10-05T12:30:00Z")["normalized_text"] == "B corrected"
    assert noc.visible_version(conn, id0, "2026-10-05T14:00:00Z")["normalized_text"] == "A original"
    # snapshot на 10:30 не видит исправление 12:00
    snap = noc.snapshot(conn, "2026-10-05T10:30:00Z")
    assert snap["items"][0]["title"] == "A original" and snap["title_corrected"] == 0
    snap2 = noc.snapshot(conn, "2026-10-05T14:30:00Z")
    assert snap2["title_corrected"] == 1 and snap2["items"][0]["version_id"] == 3


def test_upsert_late_arrival(conn):
    r = noc.upsert_item(conn, "RSS1",
                        {"title": "Late news", "url": "https://example.com/n2",
                         "published_at": "2026-10-05T08:00:00Z"},
                        "2026-10-05T10:30:00Z")
    assert r["minutes_late"] == 150 and r["late_arrival"] == 1


def test_upsert_normalizes_title(conn):
    r = noc.upsert_item(conn, "RSS1",
                        {"title": "Alpha   news", "url": "https://example.com/n3"},
                        RECEIVED)
    row = conn.execute("SELECT title FROM items WHERE id=?",
                       (r["item_id"],)).fetchone()
    assert row["title"] == "Alpha news"  # пробелы схлопнуты


# --- ingest ---------------------------------------------------------------------------

def test_ingest_bytes_rss(conn):
    noc.ensure_source(conn, "RSS1", "rss")
    res = noc.ingest_bytes(conn, "RSS1", RSS)
    assert res["ok"] and res["kind"] == "rss"
    assert res["parsed"] == 2 and res["ingested"] == 2 and res["inserted"] == 2
    res2 = noc.ingest_bytes(conn, "RSS1", RSS)  # повтор — дубликаты
    assert res2["inserted"] == 0 and res2["duplicate"] == 2


def test_ingest_bytes_moex(conn):
    noc.ensure_source(conn, "MOEXT", "moex")
    res = noc.ingest_bytes(conn, "MOEXT", MOEX)
    assert res["ok"] and res["kind"] == "moex" and res["inserted"] == 1


def test_ingest_bytes_parse_error(conn):
    noc.ensure_source(conn, "RSS1", "rss")
    res = noc.ingest_bytes(conn, "RSS1", b"not a feed")
    assert res["ok"] is False and res["error_code"] == "PARSE_ERROR"
    ev = noc.last_events(conn)["RSS1"]
    assert ev["event"] == "error" and ev["code"] == "PARSE_ERROR"


# --- snapshot / export ----------------------------------------------------------------

def test_snapshot_point_in_time(conn):
    noc.upsert_item(conn, "RSS1", {"title": "Known news", "url": "u1",
                                   "published_at": "2026-10-05T09:00:00Z"},
                    "2026-10-05T10:00:00Z")
    noc.upsert_item(conn, "RSS1", {"title": "Future received", "url": "u2",
                                   "published_at": "2026-10-05T09:30:00Z"},
                    "2026-10-05T11:00:00Z")
    noc.upsert_item(conn, "RSS1", {"title": "Future published", "url": "u3",
                                   "published_at": "2026-10-05T12:00:00Z"},
                    "2026-10-05T10:00:00Z")
    snap = noc.snapshot(conn, "2026-10-05T10:30:00Z")
    assert snap["as_of"] == "2026-10-05T10:30:00Z"
    assert snap["total_known"] == 1 and snap["by_source"] == {"RSS1": 1}
    assert snap["coverage"] == "partial_by_design"
    assert snap["published_at_future"] == 1
    assert snap["items"][0]["title"] == "Known news"


def test_snapshot_unknown_published(conn):
    noc.upsert_item(conn, "RSS1", {"title": "No pub", "url": "u4"}, RECEIVED)
    snap = noc.snapshot(conn, "2026-10-05T10:30:00Z")
    assert snap["total_known"] == 1 and snap["unknown_published_at"] == 1
    assert snap["items"][0]["published_at_unknown"] is True


def test_snapshot_source_health_by_cutoff(conn):
    noc.ensure_source(conn, "RSS1", "rss")
    noc.record_event(conn, "RSS1", "ok", "", "first", 1, at="2026-10-05T08:00:00Z")
    noc.record_event(conn, "RSS1", "error", "NETWORK", "later", 0, at="2026-10-05T12:00:00Z")
    assert noc.snapshot(conn, "2026-10-05T09:00:00Z")["source_health"]["RSS1"]["event"] == "ok"
    assert noc.snapshot(conn, "2026-10-05T13:00:00Z")["source_health"]["RSS1"]["event"] == "error"


def test_export_jsonl(conn, tmp_path):
    noc.upsert_item(conn, "RSS1", {"title": "E1", "url": "u1"}, RECEIVED)
    noc.record_event(conn, "RSS1", "ok", "", "ingest file kind=rss", 1)
    out = tmp_path / "items.jsonl"
    assert noc.export_jsonl(conn, "items", out) == 1
    line = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert line["title"] == "E1" and line["source"] == "RSS1"
    assert noc.export_jsonl(conn, "events", tmp_path / "events.jsonl") == 1
    assert noc.export_jsonl(conn, "item_versions", tmp_path / "versions.jsonl") == 1
    with pytest.raises(ValueError):
        noc.export_jsonl(conn, "bogus", tmp_path / "x.jsonl")


def test_last_events_default(conn):
    noc.ensure_source(conn, "RSS1", "rss")
    assert noc.last_events(conn)["RSS1"] == {"event": "never_seen", "code": "",
                                             "at": None, "items_count": 0}


# --- collect-once: только guard-ветки, БЕЗ сети ---------------------------------------

def test_collect_once_allowlist_guard(conn):
    res = noc.collect_once(conn, "UNKNOWN_SRC")
    assert res["ok"] is False and res["error_code"] == "NOT_IN_ALLOWLIST"
    noc.add_source(conn, "MOEX", "moex",
                   "https://iss.moex.com/iss/sitenews.json", allowed=False)
    res2 = noc.collect_once(conn, "MOEX")
    assert res2["ok"] is False and res2["error_code"] == "SOURCE_BLOCKED"


# --- адаптер DeepSeek -----------------------------------------------------------------

def test_build_analysis_input_limits():
    item = {"source": "RSS1", "published_at": "2026-10-05T09:00:00Z",
            "received_at": RECEIVED, "title": "Some title", "url": "u"}
    system, content, ih, n = noc.build_analysis_input(item)
    assert system == noc.SYSTEM_PROMPT
    assert n == len(content) <= noc.DS_MAX_INPUT_CHARS
    assert ih == hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_load_deepseek_config_from_env_file(tmp_path, monkeypatch):
    for k in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / "env"
    env.write_text("DEEPSEEK_API_KEY=sk-fake\n"
                   "DEEPSEEK_BASE_URL=https://api.example.test\n"
                   "DEEPSEEK_MODEL=deepseek-chat\n", encoding="utf-8")
    cfg = noc.load_deepseek_config(env)
    assert cfg["key"] == "sk-fake"
    assert cfg["base"] == "https://api.example.test"
    assert cfg["model"] == "deepseek-chat"


def test_load_deepseek_config_env_priority(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    env = tmp_path / "env"
    env.write_text("DEEPSEEK_API_KEY=file-key\n", encoding="utf-8")
    assert noc.load_deepseek_config(env)["key"] == "env-key"


def test_redact_url_hides_secrets():
    assert noc.redact_url("http://user:pass@proxy.local:8080/path?x=1") == "http://proxy.local:8080"
    assert noc.redact_url("https://api.deepseek.com") == "https://api.deepseek.com"


def test_analyze_item_without_key_is_unknown(conn, tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    item_id = _insert_item(conn)
    res = noc.analyze_item(conn, item_id, env_path=tmp_path / "absent.env")
    assert res["ok"] is False and res["status"] == "unknown"
    assert res["error_code"] == noc.MODEL_UNAVAILABLE
    row = conn.execute(
        "SELECT status, error_code FROM analyses "
        "WHERE item_id=? AND provider='deepseek'", (item_id,)).fetchone()
    assert row["status"] == "unknown" and row["error_code"] == noc.MODEL_UNAVAILABLE
    assert noc.analysis_for_decision(conn, item_id, FUTURE, "deepseek") is None


def test_analyze_item_provider_unsupported(conn, tmp_path):
    item_id = _insert_item(conn)
    res = noc.analyze_item(conn, item_id, provider="gpt",
                           env_path=tmp_path / "absent.env")
    assert res["error_code"] == "PROVIDER_UNSUPPORTED"


# --- валидация схемы и import-analysis ------------------------------------------------

def test_validate_analysis_ok_and_rejects():
    obj = _analysis_obj()
    title = "Sanctions review announced for X"
    assert noc.validate_analysis(obj, title) == (True, "")
    assert noc.validate_analysis(dict(obj, event_type="alien"), title) == \
        (False, "bad_enum:event_type")
    miss = {k: v for k, v in obj.items() if k != "horizon"}
    ok, why = noc.validate_analysis(miss, title)
    assert ok is False and why.startswith("missing_fields:")
    span = dict(obj, evidence_spans=["not in the title"])
    assert noc.validate_analysis(span, title) == (False, "evidence_span_not_in_title")
    assert noc.validate_analysis(dict(obj, tickers="X"), title)[0] is False  # не TypeError
    assert noc.validate_analysis(dict(obj, evidence_spans="q"), title)[1] == "bad_field:evidence_spans"
    assert noc.validate_analysis(["nope"], title) == (False, "not_object")


def test_record_imported_analysis_ok(conn):
    item_id = _insert_item(conn)
    res = noc.record_imported_analysis(conn, item_id, _analysis_obj())
    assert res["ok"] is True and res["status"] == "ok"
    row = conn.execute(
        "SELECT provider, import_kind, status, raw_content, completed_at, "
        "validation_status, input_version_id FROM analyses WHERE item_id=?",
        (item_id,)).fetchone()
    assert row["provider"] == "synthetic" and row["import_kind"] == "synthetic"
    assert row["status"] == "ok" and row["input_version_id"] is not None
    assert row["raw_content"].startswith("SYNTHETIC/OFFLINE")
    assert row["completed_at"] is not None and row["validation_status"] == "ok"
    seen = noc.analysis_for_decision(conn, item_id, FUTURE, "synthetic")
    assert seen is not None and seen["analysis"]["event_type"] == "sanctions"
    assert noc.analysis_for_decision(conn, item_id, FUTURE, "deepseek") is None


def test_record_imported_analysis_invalid_schema(conn):
    item_id = _insert_item(conn)
    res = noc.record_imported_analysis(conn, item_id,
                                       _analysis_obj(horizon="forever"))
    assert res["ok"] is False and res["error_code"] == "SCHEMA_INVALID"
    row = conn.execute("SELECT status, validation_status FROM analyses "
                       "WHERE item_id=?", (item_id,)).fetchone()
    assert row["status"] == "unknown"
    assert row["validation_status"] == "bad_enum:horizon"
    assert noc.analysis_for_decision(conn, item_id, FUTURE, "synthetic") is None


def test_analysis_bound_to_version_not_reused(conn):
    """П.2: ответ на исходную версию не используется для исправленной."""
    item_id = _insert_item(conn, title="Sanctions review announced for X")
    noc.record_imported_analysis(conn, item_id, _analysis_obj(),
                                 completed_at="2026-10-05T10:30:00Z")
    noc.upsert_item(conn, "RSS1", {"title": "Weather is warm", "url": "u9"},
                    "2026-10-05T11:00:00Z")
    # старая точка (до исправления) по-прежнему видит старый анализ
    old = noc.analysis_for_decision(conn, item_id, "2026-10-05T10:45:00Z", "synthetic")
    assert old is not None and old["input_version_id"] == 1
    # новая версия без анализа → решения нет
    assert noc.analysis_for_decision(conn, item_id, "2026-10-05T12:00:00Z", "synthetic") is None
    # повторный импорт для новой версии виден новой точке, не удаляя старый
    noc.record_imported_analysis(conn, item_id,
                                 _analysis_obj(event_type="other", entity="Moscow",
                                               evidence_spans=[]),
                                 completed_at="2026-10-05T11:30:00Z")
    assert conn.execute("SELECT COUNT(*) FROM analyses").fetchone()[0] == 2
    new = noc.analysis_for_decision(conn, item_id, "2026-10-05T12:00:00Z", "synthetic")
    assert new is not None and new["analysis"]["event_type"] == "other"


# --- shadow_decide: A/B/C, тикер, coverage (П.3) --------------------------------------

def _prep_branch(conn, title="Санкции против SBER банка", ticker="SBER",
                 external_id="1", collect_at="2026-10-05T08:10:00Z",
                 received="2026-10-05T08:00:00Z"):
    noc.ensure_source(conn, "MOEX", "moex")
    noc.record_event(conn, "MOEX", "ok", "", "collect", 1, at=collect_at)
    noc.upsert_item(conn, "MOEX", {"external_id": external_id, "title": title, "url": "u1"},
                    received)
    return noc.add_candidate(conn, ticker, "long", "2026-10-05T09:00:00Z")["candidate_id"]


def test_shadow_a_is_allow(conn):
    cid = _prep_branch(conn)
    r = noc.shadow_decide(conn, cid, "A")
    assert r["decision"] == "would_allow" and r["reason_code"] == "branch_a_no_news_veto"


def test_shadow_b_bound_risk_veto(conn):
    cid = _prep_branch(conn, title="Санкции против SBER банка", ticker="SBER")
    assert noc.shadow_decide(conn, cid, "B")["decision"] == "would_veto"


def test_shadow_b_unbound_risk_is_unknown(conn):
    cid = _prep_branch(conn, title="Санкции против GAZP банка", ticker="SBER")
    r = noc.shadow_decide(conn, cid, "B")
    assert r["decision"] == "unknown" and r["reason_code"] == "risk_unbound_ticker"


def test_shadow_b_no_collection_is_unknown(conn):
    cid = noc.add_candidate(conn, "SBER", "long", "2026-10-05T09:00:00Z")["candidate_id"]
    r = noc.shadow_decide(conn, cid, "B")
    assert r["decision"] == "unknown" and r["reason_code"] == "insufficient_coverage"


def _prep_c_with_second(conn, second_title, ticker="SBER"):
    """Кандидат SBER + первая новость (нейтрально проанализирована) + вторая новость."""
    cid = _prep_branch(conn, title="Санкции против SBER банка", ticker=ticker)
    noc.upsert_item(conn, "MOEX", {"external_id": "2", "title": second_title,
                                   "url": "u2"}, "2026-10-05T08:05:00Z")
    noc.record_imported_analysis(conn, 1, _analysis_obj(event_type="other", entity="X",
                                                        evidence_spans=[]),
                                 completed_at="2026-10-05T08:30:00Z")
    return cid


def test_shadow_c_coverage_and_veto(conn):
    cid = _prep_branch(conn, title="Санкции против SBER банка", ticker="SBER")
    # вторая новость РЕЛЕВАНТНА кандидату (называет SBER) и не проанализирована →
    # релевантное покрытие неполно. Событие про другой тикер наблюдателя сюда не
    # подходит (доказанно нерелевантно, см. test_shadow_c_other_observer_ticker_…)
    noc.upsert_item(conn, "MOEX", {"external_id": "2", "title": "Авария на SBER",
                                   "url": "u2"}, "2026-10-05T08:05:00Z")
    noc.record_imported_analysis(conn, 1, _analysis_obj(event_type="other", entity="X",
                                                        evidence_spans=[]),
                                 completed_at="2026-10-05T08:30:00Z")
    r1 = noc.shadow_decide(conn, cid, "C", provider="synthetic")
    assert r1["decision"] == "unknown" and r1["reason_code"] == "incomplete_analysis_coverage"
    assert r1["evidence"]["missing_analysis"] == 1
    assert r1["evidence"]["relevance_unestablished"] == 0
    assert r1["evidence"]["irrelevant_events"] == 1
    # подтверждённый риск-анализ (та же версия, новый ответ) приоритетнее непокрытого события
    noc.record_imported_analysis(conn, 1, _analysis_obj(evidence_spans=["Санкции против SBER"]),
                                 completed_at="2026-10-05T08:32:00Z")
    cid2 = noc.add_candidate(conn, "SBER", "long", "2026-10-05T09:00:00Z",
                             external_ref="2")["candidate_id"]
    r2 = noc.shadow_decide(conn, cid2, "C", provider="synthetic")
    assert r2["decision"] == "would_veto" and r2["reason_code"] == "analysis_risk_event"
    assert r2["evidence"]["confirmed_risk"][0]["item_id"] == 1


def test_shadow_c_other_observer_ticker_is_not_missing(conn):
    # П.1 r5: событие, называющее другой тикер наблюдателя (GAZP), доказанно
    # нерелевантно кандидату SBER → это не «непокрытый анализ», общий allow
    cid = _prep_c_with_second(conn, "Авария на GAZP")
    r = noc.shadow_decide(conn, cid, "C", provider="synthetic")
    assert r["decision"] == "would_allow" and r["reason_code"] == "no_risk_event"
    assert r["evidence"]["missing_analysis"] == 0
    assert r["evidence"]["irrelevant_events"] == 2


def test_shadow_c_unestablished_relevance_is_unknown(conn):
    # общерыночная формулировка без тикера → релевантность не установлена,
    # это НЕ молчаливый allow (в отличие от доказанно нерелевантного события)
    cid = _prep_c_with_second(conn, "Рынок акций снизился")
    r = noc.shadow_decide(conn, cid, "C", provider="synthetic")
    assert r["decision"] == "unknown" and r["reason_code"] == "relevance_unestablished"
    assert r["evidence"]["relevance_unestablished"] == 1
    assert r["evidence"]["missing_analysis"] == 0


def test_shadow_c_temporal_causality(conn):
    # completed_at анализа гейтит его видимость точке решения: анализ, завершённый
    # позже decision_time, не влияет на раннее решение и не меняет его задним числом
    noc.ensure_source(conn, "MOEX", "moex")
    noc.record_event(conn, "MOEX", "ok", "", "collect", 1, at="2026-10-05T08:10:00Z")
    noc.upsert_item(conn, "MOEX", {"external_id": "1", "title": "Санкции против SBER банка",
                                   "url": "u1"}, "2026-10-05T08:30:00Z")
    early = noc.add_candidate(conn, "SBER", "long", "2026-10-05T09:00:00Z")["candidate_id"]
    late = noc.add_candidate(conn, "SBER", "long", "2026-10-05T09:30:00Z",
                             external_ref="late")["candidate_id"]
    noc.record_imported_analysis(conn, 1, _analysis_obj(evidence_spans=["Санкции против SBER"]),
                                 completed_at="2026-10-05T09:30:00Z")
    r_early = noc.shadow_decide(conn, early, "C", provider="synthetic")
    assert r_early["decision"] == "unknown"
    assert r_early["reason_code"] == "incomplete_analysis_coverage"
    r_late = noc.shadow_decide(conn, late, "C", provider="synthetic")
    assert r_late["decision"] == "would_veto" and r_late["reason_code"] == "analysis_risk_event"
    again = noc.shadow_decide(conn, early, "C", provider="synthetic")
    assert again["created"] is False and again["decision"] == "unknown"


def test_shadow_decision_time_mismatch_and_idempotent(conn):
    cid = _prep_branch(conn)
    bad = noc.shadow_decide(conn, cid, "A", decision_time="2026-10-05T10:00:00Z")
    assert bad["error_code"] == "DECISION_TIME_MISMATCH"
    first = noc.shadow_decide(conn, cid, "A")
    again = noc.shadow_decide(conn, cid, "A")
    assert again["created"] is False and again["decision_time"] == first["decision_time"]


# --- утренняя сводка / форма разметки -------------------------------------------------

def test_morning_summary_versions(conn):
    noc.ensure_source(conn, "MOEX", "moex")
    noc.upsert_item(conn, "MOEX", {"external_id": "1", "title": "First", "url": "u1"},
                    "2026-10-05T06:00:00Z")
    noc.upsert_item(conn, "MOEX", {"external_id": "2", "title": "Second", "url": "u2"},
                    "2026-10-05T06:30:00Z")
    # исправление того же дня не должно теряться
    noc.upsert_item(conn, "MOEX", {"external_id": "2", "title": "Second fixed", "url": "u2"},
                    "2026-10-05T06:40:00Z")
    s = noc.morning_summary(conn, "2026-10-05")
    assert s["ok"] and s["items_count"] == 2 and s["corrected_count"] == 1
    # повтор идемпотентен
    assert noc.morning_summary(conn, "2026-10-05")["created"] is False


def test_label_form(conn):
    item_id = _insert_item(conn, title="Sanctions review announced for X")
    tpl = noc.label_form(conn, item_id)
    assert tpl["template"] is True and "event_type" in tpl["required"]
    bad = noc.label_form(conn, item_id, event_type="sanctions",
                         known_or_new_or_update="new", uncertainty="high",
                         evidence_quote="not present")
    assert bad["error_code"] == "BAD_EVIDENCE"
    good = noc.label_form(conn, item_id, event_type="sanctions",
                          known_or_new_or_update="new", uncertainty="high",
                          evidence_quote="Sanctions review")
    assert good["ok"] is True and good["created"] is True
    assert noc.label_form(conn, item_id)["template"] is True
