1. DeepSeek не меняет стратегию сам.
2. My3 анализирует результат и создаёт backlog.
3. My3 рекомендует один следующий шаг.
4. Мы утверждаем/отклоняем его.
5. DeepSeek выполняет только утверждённую задачу.
6. Новый run = новый run_id + manifest + config hash.
7. Legacy runs не перезаписываются.

## ФАЙЛЫ ДЛЯ ПОСТОЯННОГО ЧТЕНИЯ (в начале КАЖДОГО шага)

Это ЕДИНСТВЕННЫЙ источник состояния проекта. Новых файлов состояния не создавать —
всё новое писать в перечисленные ниже существующие файлы. Читать строго в порядке:

1. `STATUS.md` (корень, рядом с chat.md/PROJECT_STATE.md) — live handoff. ОПРЕДЕЛЯЕТ, кто действует
   (`next_action_owner`) и что делать (`next_action`). ЧИТАТЬ ПЕРВЫМ каждый шаг.
2. `chat.md` (корень проекта, рядом с PROJECT_STATE.md) — история переписки
   субагентов (My3 ↔ DeepSeek) и последние отчёты. Читать для контекста координации,
   чтобы не дублировать уже сказанное и не терять решения владельца.
3. `PROJECT_STATE.md` — canonical baseline + task queue + blocked.
   ЕДИНСТВЕННЫЙ файл, лежит в корне проекта рядом с chat.md.
   Старые копии docs/PROJECT_STATE.md и textdocs/PROJECT_STATE.md удалены.
4. `backend/reports/5b44f3b383df/research_pack_manifest.json` — baseline config hash
   `1c7f75dc44c2aa67`, commit, period, FIGIs.
5. `backend/reports/5b44f3b383df/research_pack.json` — полный pack
   (разделы A_metadata, B_strategy_config, F_econ, G_funnel — при необходимости).
6. последний отчёт эксперимента: имя брать из STATUS/PROJECT_STATE
   (сейчас `backend/reports/e1_signal_exit_202603_202604.json`;
    после E2 — `backend/reports/e2_cooldown_202605_202606_DRAFT.json`).
7. `docs/research/EXPERIMENT_QUEUE.md` — очередь пре-зарегистрированных опытов.
8. `docs/research/EXIT_ENTRY_ANALYSIS.md` — анализ входов/выходов (контекст гипотез).
9. `backend/orchestrator/executor_task.md` — ТОЛЬКО executor читает оттуда задачу.

Агент-инструкции (читать при старте сессии / инъекции промпта, НЕ на каждом шаге):
- `docs/agents/minimax/minimax/MY3_RESEARCH_LEAD.md` (My3)
- `docs/agents/minimax/minimax/MY3_GLOBAL_DISCOVERY.md` (My3, только если MODE=DISCOVERY)
- `docs/agents/minimax/minimax/RESEARCH_WORKFLOW.md` (этот файл)
- `docs/agents/minimax/minimax/DEEPSEEK_EXECUTOR.md` (executor)

ПРАВИЛО ОТСУТСТВИЯ НОВЫХ ФАЙЛОВ:
- не плодить новые файлы состояния/handoff;
- новые выводы/гипотезы/результаты — в `EXPERIMENT_QUEUE.md`,
  `EXIT_ENTRY_ANALYSIS.md` или `STATUS.md`;
- если считаешь, что нужен новый файл — сначала спроси владельца;
- baseline `strategy_id` = `ensemble_main_v1` (сверять с manifest). Расхождение
  `ensemble_main_v2` в `DEEPSEEK_EXECUTOR.md` — устаревшее, использовать v1.

Постоянные правила:
docs/agents/*.md

Текущий handoff:
PROJECT_STATE.md (корень, рядом с chat.md)

Доказательства:
reports/{run_id}/*

Конкретная задача:
docs/experiments/EXP-XXX.md

## Anti-idle conveyor

Цель: DeepSeek и My3 не должны простаивать,
но в любой момент допускается только один RUNNING
strategy experiment.

### Статусы задач

- AUDIT: исправление данных, telemetry, reconciliation, tests.
- DATA_PREP: сбор или нормализация данных, без изменения policy.
- RESEARCH: поиск методов, papers, GitHub, design experiments.
- PREPARED: код/конфиг будущего эксперимента готов, но не запущен.
- RUNNING_EXPERIMENT: единственный experiment, который меняет policy.
- REVIEW: результаты ждут проверки владельцем и My3.
- CLOSED: accepted / rejected / blocked.

### Разрешённая параллельность

Одновременно разрешены:
- 1 RUNNING_EXPERIMENT;
- любое число AUDIT, DATA_PREP, RESEARCH;
- PREPARED tasks без запуска backtest.

Запрещено:
- более одного RUNNING_EXPERIMENT;
- изменение baseline во время running experiment;
- запуск prepared config без owner approval;
- объединение результатов разных experiment run в один P&L.

### Очередь работ

1. Сначала устранить blockers и data-quality gaps.
2. Пока идёт run, готовить данные/тесты/конфиги следующей очереди.
3. После run: My3 review.
4. Только owner утверждает следующий RUNNING_EXPERIMENT.

## Handoff protocol (watchdog) — убрать реле через владельца

Цель: агенты видят завершение друг друга без пинков владельца.

- Единый live-статус: STATUS.md (корень, рядом с chat.md/PROJECT_STATE.md). ВСЕ агенты читают его в НАЧАЛЕ каждого шага.
- Владелец НЕ является реле. Агент не спрашивает "что делать?" — ответ в STATUS.
- При завершении задачи агент ПЕРЕПИСЫВАЕТ блок "Текущая очередь хода" в STATUS.md,
  указывая last_completed_by / last_task / active_RUNNING_EXPERIMENT / blocked_on /
  next_action_owner / next_action, и передаёт ход.
- DeepSeek действует только если next_action_owner == DeepSeek; иначе ждёт и не трогает
  strategy. My3 действует только если next_action_owner == My3; иначе ждёт.
- Если next_action_owner == owner — агенты останавливаются; владелец утверждает
  RUNNING_EXPERIMENT и переписывает STATUS (next_action_owner = DeepSeek).
- Перед любым запуском RUNNING_EXPERIMENT DeepSeek сверяет STATUS: E1 должен быть
  помечен APPROVED и next_action_owner == DeepSeek. Без этого — не запускать.

### Чеклист агента в начале шага
1. Прочитать STATUS.md.
2. Если next_action_owner != я — зафиксировать ожидание и остановиться (без вопросов владельцу).
3. Если == я — выполнить next_action, по завершении переписать STATUS и передать ход.