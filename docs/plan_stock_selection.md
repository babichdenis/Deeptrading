# Stock Selector Module — Final Plan

> Статус: **APPROVED** (2026-09-03)
> Цель: отбор акций и распределение капитала для V4+V2 бота
> Подход: nightly standalone module, simple-first, no ML initially

## Архитектура

```
StockSelector (отдельный модуль, НЕ часть движка)
│
├── input:  данные из БД (1m/5m candles, 160 тикеров)
├── process: liquidity → scoring → ranking → allocation
├── output: ranked candidates [{figi, side, score, size}]
├── schedule: nightly (перед торгами)
└── integration: файл/БД → движок читает кандидатов
```

## Data Split

| Период | Назначение | Месяцы |
|--------|------------|--------|
| **Train** | Калибровка scoring weights | янв–апр 2026 |
| **Validation** | Выбор лучших схем | май–июнь 2026 |
| **Test (OOS)** | Финальная проверка | август 2026 |

## Phase 0: Data Audit

**Цель:** понять что есть в БД и чего не хватает

1. Проверить покрытие: какие тикеры, какие периоды, какие интервалы
2. Посчитать `coverage_ratio` per ticker per day
3. Определить `universe_raw` (все тикеры с данными)
4. Зафиксировать правила до просмотра P&L

**Результат:** `universe_raw.json` + `data_coverage_report.md`

## Phase 1: Liquidity Filter

**Цель:** отсечь неликвидные

**Метрики (без P&L):**
- Средний дневной оборот (volume × close)
- `intrabar_range_proxy` = (high - low) / close (НЕ spread!)
- `zero_volume_rate` = доля баров с volume=0
- `coverage_ratio` = actual_bars / expected_bars per day
- `lot_accessibility` = can_buy_1_lot_at_10k_capital

**Порог (фиксировать ДО тестирования):**
- Top 40% по обороту
- `zero_volume_rate` < 10%
- `coverage_ratio` > 90%
- `lot_accessibility` = True

**Уровни universe:**
```
universe_raw:     все с данными (~160)
universe_eligible: прошли liquidity filter (~50-60)
universe_test:    eligible + полное покрытие тестового окна (~30-40)
```

**Результат:** `universe_eligible.json`, `liquidity_scores.json`

## Phase 2: Baseline on Expanded Universe (B1)

**Цель:** замерить расширенный universe БЕЗ selector

**Схема B1:**
- Все eligible тикеры
- Текущий движок V4+V2
- Fixed 10k per trade
- Сравнить с B0 (текущие 24 акции)

**Метрики:** Net, PF, WR%, DD, trades, capital utilization

**Результат:** B0 vs B1 comparison

## Phase 3: Simple Selector Baselines

**Цель:** проверить даёт ли простой ranking ценность

| Схема | Selector | Allocator |
|-------|----------|-----------|
| **B1** | Все eligible (без selector) | Fixed 10k |
| **S1** | Top-3 по momentum (return_15m) | Equal weight |
| **S2** | Top-5 по momentum | Equal weight |
| **S3** | Top-3 по signal_count | Equal weight |
| **S4** | Top-5 по signal_count | Equal weight |

**Scoring (rule-based, train period only):**
```python
momentum_score = return_15m / atr_15m * volume_ratio
signal_score = strategies_agreeing / total_strategies
score = 0.5 * norm(momentum_score) + 0.5 * norm(signal_score)
```

**Важно:** P_i, AvgWin_i, AvgLoss_i считаются ТОЛЬКО по прошлым данным (rolling window 20-50 сделок).

## Phase 4: Allocator Comparison

| Схема | Allocation |
|-------|------------|
| **A1** | Equal weight, max 35%/stock |
| **A2** | Rank-weighted (35/25/20/10), max 35% |
| **A3** | Equal weight + sector cap (50%) |
| **A4** | Equal weight + reserve (15%) |
| **A5** | Score × risk-normalized |

**Ограничения:**
- Max 35% на одну акцию
- Max 50% на один сектор
- Reserve 10-20%
- Lot-aware sizing

## Phase 5: Risk Controls

**Метрики:**
```python
position_risk = position_value * stop_distance
portfolio_risk = sum(position_risks)
sector_risk = sum(position_risks_in_sector)
```

**Лимиты:**
- Max 1% risk per position
- Max 3% portfolio risk
- Max 1.5% sector risk
- Max daily loss: 2% capital

## Phase 6: Turnover Analysis

**Метрики:**
- Rebalancing frequency
- Portfolio turnover rate
- Cost of rebalancing

**Cadence:**
- Re-rank: only on new valid signal
- Don't close position just because ranking dropped
- Let exit logic handle position closing

## Phase 7: OOS Validation

**Период:** август 2026 (не использован в train/validation)

**Сравнение:** лучшие схемы из Phase 3-6 на OOS

## Deliverables

| Файл | Описание |
|------|----------|
| `stock_selector.py` | Основной модуль |
| `liquidity_filter.py` | Фильтр ликвидности |
| `scorer.py` | Multi-factor scoring |
| `allocator.py` | Portfolio allocation |
| `universe_*.json` | Universe snapshots |
| `selector_results.json` | Результаты nightly runs |
| `backtest_selector.py` | Backtest framework |

## Integration with Bot

```
Nightly:
  StockSelector.run() → selector_results.json

Morning (before trading):
  Bot engine reads selector_results.json
  Filters signals to only trade selected tickers
  Allocates per selector recommendations
```
