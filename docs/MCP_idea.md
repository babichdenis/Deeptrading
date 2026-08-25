Да, вы понимаете направление правильно: **нейронная модель сможет управлять исследованиями через API**, перебирать заранее разрешённые параметры, запускать эксперименты, получать результаты и формировать shortlist для paper. MCP как раз подходит как стандартный слой, через который ИИ видит ресурсы и вызывает инструменты вашей системы. MCP предоставляет tools для вызова внешних действий, resources для чтения данных и prompts для стандартных исследовательских сценариев. [1019][1022][1024]

Но важная поправка:

> Нельзя позволять ИИ самостоятельно менять основной торговый движок и отправлять реальные заявки только потому, что один backtest оказался прибыльным.

ИИ должен управлять **исследовательскими конфигурациями**, а не менять математику исполнения и не получать прямой доступ к live trading без отдельных разрешений.

## Правильная схема

```text
LLM / AI
   ↓ MCP
MCP Research Server
   ↓ внутренние API
FastAPI Research API
   ↓
Experiment Manager
   ↓
Lab Worker
   ↓
Canonical Trade Engine
   ↓
Ledger / Metrics / Reports
```

MCP не заменяет FastAPI. Он становится адаптером для ИИ:

```text
AI → MCP tool → FastAPI endpoint → experiment/job → result
```

FastAPI остаётся основным API платформы, а MCP предоставляет ИИ понятные инструменты с JSON Schema.

## Что ИИ сможет делать

ИИ можно разрешить:

- узнать доступные datasets;
- получить свечи и availability;
- посмотреть каталог стратегий;
- узнать допустимые параметры;
- создать эксперимент;
- запустить матрицу параметров;
- получить progress;
- получить summary;
- получить per-FIGI/per-day metrics;
- прочитать trade ledger;
- сравнить эксперименты;
- сформировать shortlist;
- предложить следующую гипотезу;
- подготовить конфигурацию для paper.

Например, ИИ сможет сказать:

> Проверь MACD fast/slow/signal в заданной матрице, сравни ATR stop и trailing, но используй только DESIGN для исследования, отдельный VAL не трогай.

И вызовет:

```text
create_experiment_batch(...)
wait_for_job(...)
get_experiment_metrics(...)
compare_experiments(...)
```

## Что ИИ нельзя разрешать напрямую

Без ручного подтверждения ИИ не должен иметь tools для:

- изменения `trade_engine_v1`;
- изменения CostModel;
- удаления данных;
- изменения уже завершённого experiment;
- запуска live orders;
- изменения лимитов риска;
- перевода стратегии в paper/live;
- выбора VAL по одному результату;
- запуска неограниченного перебора;
- использования будущих данных;
- загрузки произвольного Python-кода;
- изменения параметров после просмотра test-результата без audit trail.

Нужны уровни доступа:

```text
READ_ONLY
RESEARCH
PAPER_CANDIDATE
PAPER
LIVE
```

Для перехода между уровнями нужен явный approval.

## Как должна работать оптимизация

Не так:

```text
AI пробует параметры
→ видит лучший результат
→ ещё сильнее подстраивает параметры
→ повторяет на том же DESIGN
```

Это быстро приведёт к переобучению.

Правильнее:

```text
1. AI формулирует гипотезу
2. Создаёт заранее ограниченную матрицу
3. DESIGN используется для исследования
4. Параметры фиксируются
5. VAL запускается один раз на зафиксированной конфигурации
6. Только после успешного VAL — paper
7. Paper проверяет реальное исполнение
8. Live требует отдельного approval
```

Количество trials нужно сохранять. Для каждого batch:

```text
batch_id
parent_hypothesis
parameter_grid
number_of_trials
dataset
engine_version
cost_model_version
selection_rule
created_at
```

ИИ должен видеть, сколько попыток уже было сделано, чтобы не считать случайный лучший результат независимым доказательством.

## Что можно оптимизировать

Через API можно разрешить перебор параметров стратегии и policy.

### MACD

```json
{
  "fast": [8, 12, 16],
  "slow": [21, 26, 34],
  "signal": [7, 9, 12]
}
```

Но добавить ограничения:

```text
fast < signal < slow
```

или хотя бы:

```text
fast < slow
```

### ATR

```json
{
  "atr_period": [10, 14, 20],
  "stop_multiplier": [1.5, 2.0, 2.5],
  "trail_activation": [0.5, 1.0],
  "trail_distance": [1.5, 2.0, 2.5]
}
```

### Noise policies

```json
{
  "opposite_confirm_bars": [1, 2, 3],
  "min_hold_bars": [0, 1, 3],
  "cooldown_bars": [0, 1, 3]
}
```

Но нельзя позволять ИИ бесконечно комбинировать всё со всем. Максимальный размер batch должен быть ограничен:

```text
max_trials_per_batch = 100
```

Для сложного поиска использовать несколько заранее определённых фаз, а не один огромный grid.

## MCP tools

Я бы создал такие tools.

### Read-only tools

```text
list_datasets
get_dataset
get_candles
get_availability
list_strategies
get_strategy_schema
list_signal_policies
list_exit_policies
get_engine_version
get_cost_model
get_experiment
get_job_status
get_metrics
get_trade_ledger
compare_experiments
read_report
```

### Research tools

```text
create_experiment
create_experiment_batch
run_experiment
cancel_experiment
wait_for_job
export_artifact
validate_experiment_config
run_qc
run_design
run_val
```

Лучше `create_experiment` сразу ставить в очередь, а отдельный `run_experiment` не нужен, если создание автоматически создаёт job. Но логика должна быть явной.

### Approval tools

```text
request_paper_candidate_approval
request_paper_run
request_live_enable
```

Эти tools не должны сами переводить систему в live без подтверждения пользователя.

## Пример MCP tool

```json
{
  "name": "create_experiment_batch",
  "description": "Create a bounded research matrix using registered strategy, signal policy, exit policy, dataset and canonical engine. Does not execute live orders.",
  "inputSchema": {
    "type": "object",
    "required": [
      "dataset_id",
      "base_config",
      "parameter_grid",
      "max_trials"
    ],
    "properties": {
      "dataset_id": {
        "type": "string"
      },
      "base_config": {
        "type": "object"
      },
      "parameter_grid": {
        "type": "object"
      },
      "max_trials": {
        "type": "integer",
        "minimum": 1,
        "maximum": 100
      },
      "purpose": {
        "type": "string",
        "enum": [
          "QC",
          "DESIGN",
          "VAL"
        ]
      }
    }
  }
}
```

Ответ:

```json
{
  "batch_id": "batch_123",
  "status": "PENDING",
  "trials": 36,
  "engine_version": "trade_engine_v1",
  "cost_model_version": "canonical_v1",
  "approval_required": false
}
```

## MCP resources

ИИ должен получать контекст через resources:

```text
resource://project/specification
resource://engine/canonical/v1
resource://datasets/{dataset_id}/metadata
resource://experiments/{experiment_id}/summary
resource://experiments/{experiment_id}/report
resource://experiments/{experiment_id}/ledger
resource://research/roadmap
resource://research/selection-rules
```

В resource `project/specification` описать:

- что такое DESIGN;
- что такое VAL;
- требования к costs;
- правила no-lookahead;
- критерии REJECT;
- критерии candidate;
- запрет live без approval;
- лимиты матриц.

## MCP prompts

Полезно создать стандартные prompts:

```text
research_new_strategy
audit_experiment
compare_parameter_batch
prepare_val_candidate
review_rejected_hypothesis
prepare_paper_candidate
```

Например, `audit_experiment` заставляет ИИ проверить:

- net;
- PF;
- H1/H2;
- per-FIGI;
- net without top1;
- drawdown;
- costs;
- data quality;
- look-ahead;
- число trials.

## Workflow для нейронки

### Шаг 1. Идея

ИИ предлагает:

```text
strategy: pullback_to_ema
hypothesis: входить в откат внутри направленного тренда
```

### Шаг 2. Проверка возможности

ИИ через MCP получает:

- есть ли нужные timeframe;
- есть ли volume;
- есть ли VWAP;
- есть ли данные по всем FIGI;
- доступен ли short;
- есть ли нужная session metadata.

Если данных нет — стратегия не запускается.

### Шаг 3. QC

Один день, 1–3 FIGI:

```text
purpose = QC
```

ИИ получает ledger и проверяет механику.

### Шаг 4. DESIGN

Запуск полного DESIGN с зафиксированной конфигурацией.

### Шаг 5. Analysis

ИИ получает:

- summary;
- metrics;
- trades;
- report;
- artifact.

И формирует verdict:

```text
REJECT
INCONCLUSIVE
DESIGN_CANDIDATE
```

Но он не должен сам объявлять VAL без policy gate.

### Шаг 6. VAL

Для VAL:

- параметры уже frozen;
- нельзя изменять их после просмотра VAL;
- новый experiment должен ссылаться на исходный DESIGN batch;
- фиксируется дата создания VAL;
- сохраняется audit trail.

### Шаг 7. Paper

Paper использует те же:

- strategy version;
- policy version;
- exit version;
- engine version;
- cost model;
- risk limits.

Отличается только execution adapter.

### Шаг 8. Live

Live включается отдельно:

```text
RESEARCH
→ DESIGN_CANDIDATE
→ VAL_PASS
→ PAPER_PASS
→ MANUAL_APPROVAL
→ LIVE
```

ИИ не должен перескакивать через этапы.

## Нейронка не должна менять основной движок

Основной движок должен быть versioned и защищён:

```text
trade_engine_v1
trade_engine_v2
```

Если обнаружена ошибка:

1. Создаётся issue.
2. Исправляется код.
3. Добавляются regression tests.
4. Выпускается новая версия engine.
5. Все старые experiments сохраняют ссылку на старую версию.
6. Старые результаты не переписываются молча.

ИИ может предложить изменение engine, но не применять его автоматически.

## Автоматическое изменение параметров

Да, ИИ может подбирать параметры, но только в безопасной форме:

```text
parameter proposal
→ schema validation
→ allowed bounds
→ max trials
→ immutable batch
→ metrics
→ selection rule
```

Выбор должен учитывать не только net:

```text
score =
  net
  - drawdown_penalty
  - concentration_penalty
  - turnover_penalty
```

Но даже score не должен использоваться как доказательство live edge. Лучший score — это только кандидат для следующего этапа.

## Пример запроса от ИИ

```json
{
  "strategy": {
    "id": "macd_cross",
    "version": "1.0.0",
    "params": {
      "fast": 12,
      "slow": 26,
      "signal": 9
    }
  },
  "signal_policy": {
    "id": "confirmed_flip",
    "version": "1.0.0",
    "params": {
      "confirm_bars": 2
    }
  },
  "exit_policy": {
    "id": "atr_trailing",
    "version": "1.0.0",
    "params": {
      "atr_period": 14,
      "initial_stop_atr": 2.0,
      "trail_activation_atr": 1.0,
      "trail_distance_atr": 2.0
    }
  },
  "dataset_id": "design_20260615_20260714_5m",
  "purpose": "DESIGN",
  "max_trials": 50
}
```

## FastAPI и MCP

MCP server может быть отдельным сервисом:

```text
mcp-server
  ↓ HTTP internal API
fastapi
  ↓
research services
```

Или MCP tool handler может вызывать Python-сервисы напрямую. Но для вашего проекта лучше:

```text
AI
 ↓ MCP
MCP adapter
 ↓
FastAPI internal API
 ↓
Lab/Research services
```

Преимущества:

- FastAPI остаётся единой точкой бизнеса;
- MCP не дублирует логику;
- UI, CLI и AI используют один API;
- права доступа проще контролировать;
- можно отключить MCP, не ломая Lab.

MCP должен быть «интерфейсом управления и чтения», а не вторым отдельным движком.

## Главная опасность

Самая большая опасность — автоматический перебор и выбор параметров по DESIGN:

```text
ИИ перебрал 10 000 комбинаций
→ нашёл одну прибыльную
→ назвал её стратегией
```

Это почти наверняка переобучение. Даже если ИИ не видит будущее напрямую, он может многократно использовать один и тот же тестовый период.

Поэтому система должна обязательно считать:

- количество trials;
- dataset reuse;
- версию параметров;
- родительскую гипотезу;
- selection rule;
- число итераций;
- период, использованный для разработки;
- отдельный VAL;
- paper performance.

Walk-forward и out-of-sample-проверка должны быть обязательной частью pipeline, а не добровольной настройкой. [arxiv](https://arxiv.org/html/2512.12924v1)

## Итоговая архитектура

```text
LLM
  ↓ MCP
MCP Server
  ├── read resources
  ├── research tools
  ├── approval tools
  └── prompts
       ↓
FastAPI
  ├── Warehouse API
  ├── Strategy Registry
  ├── Policy Registry
  ├── Experiment API
  ├── Job API
  ├── Results API
  └── Paper/Live Approval API
       ↓
Lab Workers
  ├── Strategy
  ├── Signal Policy
  ├── Position Engine
  ├── Execution Model
  ├── Cost Model
  └── Metrics
       ↓
PostgreSQL + Parquet + Redis
```

Коротко:

> Да, нейронка сможет через MCP запускать тесты, менять допустимые параметры, сравнивать стратегии и готовить кандидатов для paper. Но MCP должен вызывать ваш versioned FastAPI Research API, а не менять код напрямую. Основной engine и CostModel должны быть защищены, каждый experiment — immutable, matrix — ограниченной, а переход в paper/live — проходить через отдельные gates и ручное подтверждение.