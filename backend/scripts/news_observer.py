#!/usr/bin/env python3
"""A6 news_observer CLI: локальный архив новостей + теневой анализ.

По умолчанию сети НЕТ. Сеть — только в явно вызванных командах:
  collect-once   — ОДИН ограниченный read-only сбор ОДНОГО источника из allowlist;
  analyze-once   — ОДИН DeepSeek-запрос по архивированной новости
                   (лимиты сдачи: input<=8000 симв., max_output_tokens<=1000,
                   timeout 30с, retries=0). Ошибки → UNKNOWN, не подмена успехом.

Команды:
  ingest          — инжест СОХРАНЁННОГО RSS/XML/MOEX JSON (без сети);
  collect-once    — сбор одного явно перечисленного источника (сеть!);
  snapshot        — точка во времени --as-of (UTC, без сети);
  export          — JSONL-экспорт: items | item_versions | analyses | events |
                    candidates | shadow_decisions | morning_summaries | labels;
  analyze-once    — один реальный LLM-запрос (сеть!);
  import-analysis — импорт сохранённого ответа модели (SYNTHETIC/OFFLINE, без сети);
  candidate       — п.5: сохранить кандидата входа (без ордеров, без сети);
  shadow-decide   — п.5: теневое решение A/B/C по кандидату (без сети);
  morning-summary — п.3: утренняя сводка на дату (cutoff 09:45 MSK, без сети);
  label-form      — п.6: форма ручной разметки (без полей — шаблон, без сети);
  net-diag        — п.5: офлайн-проверка proxy/base (тип/host/port, без секретов);
  migrate-archive — п.4: ЯВНАЯ миграция старого архива ТОЛЬКО в новый файл;
  sources         — список источников и последние события.

Путь архива (--db) обязателен и только на локальном диске хоста (НЕ SMB //Volumes).
Ключи/headers никогда не пишутся в архив и в вывод. Постоянный процесс НЕ запускается.

Примеры запуска на сервере (архив — локальный путь):
  python3 scripts/news_observer.py ingest --db /var/lib/deeptrading/news_a6.db \
      --source "Ведомости" --file /tmp/vedomosti_20261008.xml
  python3 scripts/news_observer.py collect-once --db /var/lib/deeptrading/news_a6.db \
      --source MOEX --limit 20
  python3 scripts/news_observer.py snapshot --db /var/lib/deeptrading/news_a6.db \
      --as-of 2026-10-08T12:00:00Z --out /tmp/snapshot.json
  python3 scripts/news_observer.py export --db /var/lib/deeptrading/news_a6.db \
      --kind items --out /tmp/items.jsonl
  python3 scripts/news_observer.py analyze-once --db /var/lib/deeptrading/news_a6.db \
      --item-id 42
  python3 scripts/news_observer.py import-analysis --db /var/lib/deeptrading/news_a6.db \
      --item-id 42 --file /tmp/analysis.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import news_observer_core as core  # noqa: E402


def _print(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="news_observer", description="A6: архив новостей + теневой анализ (MVP)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_db(p: argparse.ArgumentParser) -> None:
        p.add_argument("--db", required=True,
                       help="путь SQLite-архива (локальный диск, НЕ SMB)")

    p = sub.add_parser("ingest", help="инжест сохранённого RSS/XML/MOEX JSON (без сети)")
    add_db(p)
    p.add_argument("--source", required=True, help="имя источника в архиве")
    p.add_argument("--file", required=True, help="путь к сохранённому файлу")
    p.add_argument("--limit", type=int, default=1000)

    p = sub.add_parser("collect-once",
                       help="ОДИН ограниченный сбор одного источника из allowlist (сеть)")
    add_db(p)
    p.add_argument("--source", required=True,
                   help="один из: " + ", ".join(sorted(core.KNOWN_SOURCES)))
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("snapshot", help="точка во времени --as-of (без сети)")
    add_db(p)
    p.add_argument("--as-of", dest="as_of", required=True,
                   help="ISO UTC, например 2026-10-08T12:00:00Z")
    p.add_argument("--source", default=None)
    p.add_argument("--out", default=None, help="записать полный snapshot в файл")
    p.add_argument("--max-items", type=int, default=200)

    p = sub.add_parser("export", help="JSONL-экспорт (без сети)")
    add_db(p)
    p.add_argument("--kind", required=True,
                   choices=["items", "item_versions", "analyses", "events",
                            "candidates", "shadow_decisions", "morning_summaries",
                            "labels"])
    p.add_argument("--out", required=True)

    p = sub.add_parser("analyze-once",
                       help="ОДИН DeepSeek-запрос по архивированной новости (сеть)")
    add_db(p)
    p.add_argument("--item-id", type=int, required=True)
    p.add_argument("--provider", default="deepseek", choices=["deepseek"])
    p.add_argument("--env", default=None, help="путь к .env (по умолчанию backend/.env)")

    p = sub.add_parser("import-analysis",
                       help="импорт сохранённого ответа модели (SYNTHETIC/OFFLINE, без сети)")
    add_db(p)
    p.add_argument("--item-id", type=int, required=True)
    p.add_argument("--file", required=True, help="JSON сохранённого структурированного ответа")
    p.add_argument("--completed-at", dest="completed_at", default=None,
                   help="офлайн-реплей: время готовности анализа (ISO UTC)")
    p.add_argument("--version-id", dest="version_id", type=int, default=None,
                   help="офлайн-реплей: привязать к конкретной версии входа")

    p = sub.add_parser("candidate",
                       help="п.5: сохранение кандидата входа (без ордеров, без сети)")
    add_db(p)
    p.add_argument("--ticker", required=True)
    p.add_argument("--side", required=True, help="long | short")
    p.add_argument("--decision-time", dest="decision_time", required=True,
                   help="ISO-время решения (нормализуется в UTC)")
    p.add_argument("--price-strategy-version", dest="price_strategy_version", default="")
    p.add_argument("--data-version", dest="data_version", default="")
    p.add_argument("--external-ref", dest="external_ref", default="")

    p = sub.add_parser("shadow-decide",
                       help="п.5: теневое решение A/B/C по кандидату (без сети)")
    add_db(p)
    p.add_argument("--candidate-id", type=int, required=True)
    p.add_argument("--branch", required=True, help="A | B | C")
    p.add_argument("--decision-time", dest="decision_time", default="",
                   help="необязательно; по умолчанию decision_time кандидата")
    p.add_argument("--provider", default="deepseek",
                   help="C: провайдер анализа (deepseek — перспектива; synthetic — офлайн)")

    p = sub.add_parser("morning-summary",
                       help="п.3: утренняя сводка (cutoff 09:45 MSK; без сети)")
    add_db(p)
    p.add_argument("--date-msk", dest="date_msk", default="",
                   help="дата YYYY-MM-DD; по умолчанию — сегодня (MSK)")

    p = sub.add_parser("label-form",
                       help="п.6: форма разметки (без полей — шаблон; без сети)")
    add_db(p)
    p.add_argument("--item-id", type=int, required=True)
    p.add_argument("--labeler", default="manual")
    p.add_argument("--event-type", dest="event_type", default="")
    p.add_argument("--entity", default="")
    p.add_argument("--known-or-new-or-update", dest="known_or_new_or_update", default="")
    p.add_argument("--evidence-quote", dest="evidence_quote", default="",
                   help="точная подстрока заголовка (доказательство)")
    p.add_argument("--uncertainty", default="")

    p = sub.add_parser("sources", help="список источников и последние события")
    add_db(p)

    p = sub.add_parser("net-diag",
                       help="п.5: офлайн-диагностика proxy/base (без сети, без секретов)")
    p.add_argument("--env", default=None, help="путь к .env (по умолчанию backend/.env)")

    p = sub.add_parser("migrate-archive",
                       help="п.4: явная миграция старого архива в НОВЫЙ файл (без сети)")
    p.add_argument("--src", required=True, help="старый архив (read-only, не изменяется)")
    p.add_argument("--out", required=True, help="новый файл архива (должен быть пуст/отсутствовать)")

    args = ap.parse_args(argv)
    if args.cmd == "net-diag":
        _print(core.deepseek_network_diag(args.env))
        return 0
    if args.cmd == "migrate-archive":
        res = core.migrate_legacy_archive(args.src, args.out)
        _print(res)
        return 0 if res.get("ok") else 3
    try:
        conn = core.connect(args.db)
    except ValueError as e:
        _print({"ok": False, "error_code": "ARCHIVE_SCHEMA_UNSUPPORTED",
                "detail": core.sanitize_text(str(e), 300)})
        return 3
    rc = 3
    try:
        if args.cmd == "ingest":
            core.ensure_source(conn, args.source, "import")
            data = Path(args.file).read_bytes()
            res = core.ingest_bytes(conn, args.source, data, limit=args.limit)
            _print(res)
            rc = 0 if res.get("ok") else 2
        elif args.cmd == "collect-once":
            res = core.collect_once(conn, args.source, limit=args.limit)
            _print(res)
            rc = 0 if res.get("ok") else 2
        elif args.cmd == "snapshot":
            snap = core.snapshot(conn, args.as_of, source=args.source)
            if args.out:
                Path(args.out).write_text(json.dumps(snap, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
            items = snap.get("items", [])
            summary = {k: v for k, v in snap.items() if k != "items"}
            summary["items_total"] = len(items)
            if not args.out:
                keep = max(0, args.max_items)
                summary["items"] = items[:keep]
                summary["items_truncated"] = max(0, len(items) - keep)
            else:
                summary["snapshot_file"] = str(args.out)
            _print(summary)
            rc = 0
        elif args.cmd == "export":
            n = core.export_jsonl(conn, args.kind, args.out)
            _print({"ok": True, "kind": args.kind, "rows": n, "out": str(args.out)})
            rc = 0
        elif args.cmd == "analyze-once":
            res = core.analyze_item(conn, args.item_id, provider=args.provider,
                                    env_path=args.env)
            _print(res)
            rc = 0 if res.get("ok") else 2
        elif args.cmd == "import-analysis":
            obj = json.loads(Path(args.file).read_text(encoding="utf-8"))
            res = core.record_imported_analysis(conn, args.item_id, obj,
                                                completed_at=args.completed_at,
                                                version_id=args.version_id)
            _print(res)
            rc = 0 if res.get("ok") else 2
        elif args.cmd == "candidate":
            res = core.add_candidate(conn, args.ticker, args.side, args.decision_time,
                                     price_strategy_version=args.price_strategy_version,
                                     data_version=args.data_version,
                                     external_ref=args.external_ref)
            _print(res)
            rc = 0 if res.get("ok") else 2
        elif args.cmd == "shadow-decide":
            res = core.shadow_decide(conn, args.candidate_id, args.branch,
                                     decision_time=args.decision_time,
                                     provider=args.provider)
            _print(res)
            rc = 0 if res.get("ok") else 2
        elif args.cmd == "morning-summary":
            res = core.morning_summary(conn, args.date_msk)
            _print(res)
            rc = 0 if res.get("ok") else 2
        elif args.cmd == "label-form":
            res = core.label_form(conn, args.item_id, labeler=args.labeler,
                                  event_type=args.event_type, entity=args.entity,
                                  known_or_new_or_update=args.known_or_new_or_update,
                                  evidence_quote=args.evidence_quote,
                                  uncertainty=args.uncertainty)
            _print(res)
            rc = 0 if res.get("ok") else 2
        elif args.cmd == "sources":
            _print({"sources": core.list_sources(conn),
                    "last_events": core.last_events(conn)})
            rc = 0
    except ValueError as e:
        _print({"ok": False, "error_code": "USAGE_ERROR",
                "detail": core.sanitize_text(str(e), 200)})
        rc = 3
    finally:
        conn.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
