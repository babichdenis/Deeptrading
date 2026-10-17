# AGENTS.md — правила работы в Deeptrading

Сначала прочитать [MEMORY.md](MEMORY.md). Единая база знаний — [docs/knowledge/README.md](docs/knowledge/README.md); текущий статус — [CURRENT_STATE](docs/knowledge/CURRENT_STATE.md). Общение workers — только корневой [chat.md](chat.md).

## Рабочая среда

- Основной checkout — на сервере .7 в `/Users/Denis/Dev/Deeptrading` (локальный диск; с .6 он виден как SMB-шара `/Volumes/Dev/Deeptrading`). Там же backend/frontend и боевая PostgreSQL 16 в Docker-контейнере `deeptrading-db` (`127.0.0.1:5432`, БД `deeptrading`). Mac «код» — .6.
- Рабочая копия на Windows .8: проект `X:\Deeptrading` и своя PostgreSQL 16.15 на `127.0.0.1:5432` (роль и БД `deeptrading`); восстановлена из дампа .7 2026-10-17. Локальный PG14 на .6 — песочница, не источник данных. Старая IP-карта .2/.3 не актуальна. Подробности: [OPERATIONS](docs/knowledge/OPERATIONS.md).
- Не устанавливать venv/node_modules через SMB. Python окружение локально `~/.venvs/deeptrading`; npm install/ci — только там, где диск проекта локальный.
- Изменения на шаре видны серверу сразу и могут подхватиться autoreload. Не затирать чужие файлы/процессы. Если шара отключена, не продолжать на старой локальной копии.
- Пароли, токены и account secrets — в непубликуемом окружении/хранилище, не в правилах, отчётах или CLI-логах. SDK: `t-tech-investments`, импорт `t_tech.invest`.

## Действующие ограничения

- Не запускать бот, торговлю, деплой, брокерские API, backfill и запись рабочей БД по старому handoff. Выполнять только в пределах актуального разрешения пользователя/назначения. Текущие PARALLEL-DAY пакеты этих операций не назначают.
- Тесты запускать и добавлять в согласованном scope. Новый engine-код требует golden-покрытия; не расширять прогон до всего suite без запроса.
- Не делать reset/clean/откат чужой работы. При SIGBUS git на SMB сохранять файловое происхождение и работать с локальным серверным checkout при необходимости.

## Неизменяемые технические контракты

- Время UTC, tz-aware; MSK на границе UI/явной сессионной политики. Метка свечи — начало `[T,T+TF)`, видимость `T+TF <= as_of`. [ADR-0002](docs/adr/0002-timeframe-semantics.md) описывает отдельно runtime/feed clock: `_is_closed` изолированно не менять.
- Исторические потребители берут свечи из БД; источник → проверка/ensure недостающего → PostgreSQL → потребитель. Пропуск, ошибка источника и отсутствие торгов — разные состояния.
- Закрытый бар → policy/audit → next open, slippage против сделки. Stop/target conflict → STOP_LOSS_FIRST, gap-through-stop → open; конец данных обрабатывается явно, цену не выдумывать.
- Один FIGI — одна позиция, без усреднения/пирамидинга. Unknown outcome не равен нулю; closed cashflows не заменяют неизвестный капитал. [EXECUTION](docs/knowledge/EXECUTION.md).
- Replay/parity прогреваются так же, как runtime: около 3 дней 1m из БД, ensemble ≥120 баров. Холодный replay не доказывает отсутствие сигналов.
- Результат воспроизводим по input/config/code hashes и fingerprint ledger. Признаки не видят будущее; freeze до eval; известные defects/ограничения не скрывать.
- Комментарии в новом коде запрещены: понятные имена, docstrings только там, где критично. Денежные расчёты исследовательского engine — float.
- На `(method,path)` один API-handler. Базовые контуры и точные команды — [TESTING](docs/knowledge/TESTING.md), [PIPELINE_TESTING](docs/PIPELINE_TESTING.md).

## Совместная работа и документы

- Полные текущие зоны и задания: [PARALLEL_DAY_1](docs/PARALLEL_DAY_1.md). Не править чужие зоны и общий frozen reference. Прочитать новые сообщения своей задачи до начала.
- `chat.md` append-only. `.chat-write.lock` — постоянный файл: open(a), exclusive `fcntl.flock`, append+flush/fsync+readback, unlock. Не удалять и не заменять mkdir-lock.
- Текущий статус обновлять в `docs/knowledge/CURRENT_STATE.md`; MEMORY остаётся коротким входом. Завершённый блок — в `docs/PROGRESS.md`; постоянное знание — в соответствующей теме. Не дублировать всю хронику.
- Старые отчёты/архивы не являются текущими назначениями. Прибыльность и доступность сервисов не устанавливаются названием файла или старым положительным net.
- Новый документ включать в [DOCUMENT_INDEX](docs/knowledge/DOCUMENT_INDEX.md). Не менять старые run-артефакты. Полный процесс: [WORKFLOW](docs/knowledge/WORKFLOW.md).
