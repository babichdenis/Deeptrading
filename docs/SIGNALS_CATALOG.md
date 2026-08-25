# Дизайн: Каталог стратегий и «сигналы как данные»

> Статус: утверждённая архитектура этапа «Каталог». Тесты (Lab) НЕ запускаем,
> пока сигналы не увидим глазами на графике Сбербанка.

## 1. Главный принцип

**Сигналы = данные, как свечи.**

```
стратегия × параметры × figi × интервал → считается ОДИН РАЗ → лежит в PostgreSQL
график / отчёты / кворум / будущий оркестр → читают из базы
```

Пересчёт только если: набор параметров новый (hash не найден) или force=true.
Это отвечает на требование «чтобы 100 раз не гонять»: смена SL/TP/трейлинга/кворума
НЕ пересчитывает стратегии — эти слои применяются поверх уже сохранённых сигналов.

## 2. Каталог: 15 стратегий входа

Формализация правил — из main_plan-обсуждения. Все стратегии работают только
по ЗАКРЫТЫМ барам; исполнение всегда на open следующего бара (canonical timing).

### Волна 1 (реализуем первыми)

| id | Название | Семейство | Long | Short |
|----|----------|-----------|------|-------|
| `rsi_reversal` | RSI reversal | reversal | RSI<35 И RSI растёт И close>high[t-1] | RSI>65 И падает И close<low[t-1] |
| `bollinger_reclaim` | Bollinger reclaim | reversal | close[t-1]<нижняя полоса И close[t]>нижней полосы | зеркально от верхней |
| `pullback_ema` | Pullback to EMA | pullback | EMA50 растёт, цена выше EMA50, low коснулся EMA20, close>high[t-1] | зеркально |
| `vwap_reclaim` | VWAP reclaim (intraday) | reversal | цена < VWAP−k·σ, затем close>VWAP | цена > VWAP+k·σ, затем close<VWAP |
| `range_compression_breakout` | Squeeze breakout | breakout | ATR/BB-width в низшем перцентиле N барсов → пробой верха диапазона | пробой низа |

### Волна 2

`higher_low_structure`, `sr_reclaim`, `fib_pullback`, `volume_breakout`, `opening_range_breakout`

### Волна 3

`zscore_reversal`, `rsi_divergence`, `momentum_burst`, `relative_strength`
(+ `donchian_breakout` и `macd_cross` уже готовы как baseline)

### Карточка стратегии (отдаётся фронту)

```json
{
  "id": "rsi_reversal",
  "name": "RSI Reversal",
  "family": "reversal",
  "wave": 1,
  "status": "AVAILABLE",
  "timeframes": ["5min", "15min", "hour", "day"],
  "long_rule": "RSI<35 & RSI↑ & close>high[t-1]",
  "short_rule": "RSI>65 & RSI↓ & close<low[t-1]",
  "params_schema": {
    "period": {"type": "int", "default": 14},
    "oversold": {"type": "float", "default": 35},
    "overbought": {"type": "float", "default": 65}
  },
  "defaults": {"period": 14, "oversold": 35, "overbought": 65}
}
```

## 3. Слои ПОВЕРХ сигналов (не входят в пересчёт стратегий)

| Слой | Что это | Влияет на сигналы в базе? |
|------|---------|---------------------------|
| Signal policies | ignore_same_side, min_hold, session cutoff | Нет — помечают статус CANDIDATE→ACCEPTED/REJECTED при прогоне через движок |
| Exit policy | fixed_sl_tp / atr_stop / atr_trailing / off | Нет — только Lab/PnL |
| Размер позиции | фикс qty / % риска | Нет — только Lab/PnL |
| Фильтры режима | тренд EMA50, ADX, ATR-percentile | Позже: отдельный policy-слой v2 |

## 4. Кворум / оркестр

Каждая стратегия хранит свои сигналы независимо. **Оркестр — слой чтения:**

```json
{
  "strategy_id": "quorum_2of3",
  "params": {
    "members": ["rsi_reversal", "bollinger_reclaim", "pullback_ema"],
    "k": 2,
    "window_bars": 0
  }
}
```

**Решение по окну кворума:** v1 — строго `window_bars = 0` (same-bar):
«сколько стратегий одновременно увидели одну и ту же возможность на одном
и том же закрытом баре». Симметричное ±N окно для торговых сигналов запрещено.

Будущее расширение — только каузальное назад:

```json
{"lookback_window_bars": 2, "forward_window_bars": 0}
```

`forward_window_bars` всегда 0 для торговых сигналов. Hindsight-выравнивание —
отдельный режим визуального анализа, с торговым кворумом не смешивается.

Кворум-run обязан хранить зависимости для воспроизводимости:

```sql
run_dependencies (
  parent_run_id UUID REFERENCES strategy_runs(id) ON DELETE CASCADE,
  child_run_id  UUID REFERENCES strategy_runs(id),
  role VARCHAR(32)
)
```

Иначе через месяц изменённые параметры участника сделают quorum-run необъяснимым.

## 4.1 Разделение слоёв: signals vs decisions

`signals` — ТОЛЬКО сырые сигналы стратегии (статусы `CANDIDATE`, `EXIT`).
Применение политик (ACCEPTED / REJECTED / IGNORED + причина) — отдельный слой
этапа 2, отдельная таблица `signal_decisions(signal_id, decision, policy_id,
reason_code, details)`. Не смешивать: raw signal ≠ accepted ≠ rejected ≠ exit.
Каждую отфильтрованную свечу в signals НЕ пишем — база не должна распухать.

Именование времени: `signal_ts` = время закрытия бара-инициатора.
`execution_ts` (open следующего бара) существует только в Lab/execution слое.
Сигнал НЕ содержит цену входа.

## 5. Модель данных

```sql
strategy_runs (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  figi          VARCHAR(32) NOT NULL,
  interval_name VARCHAR(16) NOT NULL,
  strategy_id   VARCHAR(64) NOT NULL,
  strategy_version VARCHAR(16) NOT NULL,
  engine_version VARCHAR(32) NOT NULL,    -- trade_engine_v1
  data_version   CHAR(16) NOT NULL,       -- hash покрытия свечей (см. §5.1)
  params        JSONB NOT NULL DEFAULT '{}',
  params_hash   CHAR(16) NOT NULL,      -- sha256(canonical_params)[:16]
  bars          INT NOT NULL,
  from_ts       TIMESTAMPTZ NOT NULL,
  to_ts         TIMESTAMPTZ NOT NULL,
  created_at    TIMESTAMPTZ DEFAULT now(),
  UNIQUE (figi, interval_name, strategy_id, params_hash)
)

signals (
  id       BIGSERIAL PRIMARY KEY,
  run_id   UUID NOT NULL REFERENCES strategy_runs(id) ON DELETE CASCADE,
  figi     VARCHAR(32) NOT NULL,
  ts       TIMESTAMPTZ NOT NULL,        -- время закрытия бара-инициатора
  side     VARCHAR(8) NOT NULL,         -- BUY | SELL
  status   VARCHAR(32) NOT NULL,        -- CANDIDATE | EXIT
  reason   TEXT NOT NULL DEFAULT '',
  features JSONB NOT NULL DEFAULT '{}',
  UNIQUE (run_id, ts, side)
)
CREATE INDEX ON signals (figi, ts);
```

`params_hash` = sha256 от канонического JSON параметров (sorted keys) [:16].

### 5.1 Data version — обязательная часть cache key

Cache key: `(figi, interval_name, strategy_id, strategy_version, params_hash,
engine_version, data_version)`.

`data_version` = sha16 от покрытия свечей `(bars, min_ts, max_ts, sum_close)`.
Пришли новые бары → data_version изменился → старый run молча НЕ используется:
создаётся новый run, прежний удаляется (cascade signals), в ответе
`replaced_stale: true`. Семантика ответа однозначна:

- `cached: true` — exact match по всем компонентам ключа, 0 расчётов
- `cached: false` — выполнен расчёт

## 6. API v1

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/v1/strategies/catalog` | карточки всех стратегий + статус волны |
| POST | `/api/v1/signals/compute` | ensure_candles → свечи из БД → расчёт → upsert → `{run_id, count, cached}` |
| GET | `/api/v1/signals/{run_id}` | сигналы прогона |
| GET | `/api/v1/runs?figi=&interval_name=` | история прогонов |
| POST | `/api/v1/quorum/compute` | голосование поверх сохранённых runs |

Пример compute:

```json
POST /api/v1/signals/compute
{
  "figi": "BBG004730N88",
  "interval_name": "hour",
  "strategy_id": "rsi_reversal",
  "params": {"period": 14, "oversold": 35},
  "days": 120,
  "force": false
}
```

Ответ:

```json
{
  "run_id": "6f1c…",
  "cached": false,
  "count": 17,
  "signals": [
    {"ts": "2026-08-20T14:00:00Z", "side": "BUY", "status": "CANDIDATE",
     "reason": "rsi_turn_up", "features": {"rsi": 31.2}}
  ]
}
```

## 7. Frontend

Нижняя панель (вместо заглушки), две вкладки:

**«Стратегии»**
- карточки каталога: чекбокс, имя, семейство, бейдж волны/статуса
- у выбранных — инлайн-параметры (из params_schema)
- пресеты кнопками: `[Контртренд]` `[Тренд]` `[Волна 1 целиком]`
- выбор exit-пресета (для будущего Lab): fixed_sl_tp / atr_stop / trailing
- кнопка **«Показать на графике»** → compute по выбранным → маркеры:
  - ▲ BUY accepted (зелёный), ▽ SELL accepted (красный)
  - серые точки — rejected, причина в тултипе
- тумблеры слоёв: raw setups / accepted / rejected / exits

**«Отчёты»**
- таблица прогонов (run_id, стратегия, параметры-hash, кол-во сигналов, когда)
- клик → summary: raw/accepted/rejected, топ причин
- позже сюда же лягут отчёты Lab

Статус-бар показывает: `rsi_reversal hour: 17 сигналов (из кеша)` либо `(рассчитано заново)`.

## 8. Порядок работ (этап «Каталог»)

1. ✅ ORM: strategy_runs + signals (+create_all, Alembic позже)
2. ✅ Каталог-метаданные в коде + GET /catalog
3. 🔄 Волна 1: пять стратегий как чистые генераторы сигналов + golden-тесты правил
4. ⏳ POST /signals/compute + ensure_signals (кеш) + чтение
5. ⏳ Фронт-панель: каталог, пресеты, маркеры, отчёты
6. ⏳ Кворум как мета-стратегия
7. ⏳ Только после визуального QC → Lab (SL/TP/trailing/sizing/experiments)

## 9. Интеграция ИИ через MCP (см. docs/MCP_idea.md)

Архитектура совместима по построению. Что уже заложено:

| Требование MCP-схемы | Чем обеспечивается |
|---|---|
| ИИ читает всё через API | FastAPI + /openapi.json (машиночитаемая схема) |
| Воспроизводимость | params_hash, engine_version в каждом run, fingerprint ledger |
| Сигналы не пересчитываются зря | кеш signal_sets — ИИ может «перебирать» дёшево |
| Разрешённые параметры и границы | params_schema (min/max) в каталоге → валидация в compute |
| Защита движка | ENGINE_ID=trade_engine_v1 в engine/version.py; версии immutable |

Что добавим на этапах Lab/Orchestrator (пока НЕ делаем):

- purpose (QC/DESIGN/VAL) + счётчик trials + parent_hypothesis у экспериментов
- create_experiment_batch с max_trials ≤ 100 и constraint'ами сетки (fast<slow)
- уровни доступа READ_ONLY→RESEARCH→PAPER_CANDIDATE→PAPER→LIVE + approval gates
- MCP-адаптер как тонкая обёртка над FastAPI (отдельный процесс, отключаемый)

Правило: ИИ предлагает конфигурации и видит результаты; изменение движка,
CostModel и переходы paper/live — только через человека.
