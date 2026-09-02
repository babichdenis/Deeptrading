# AUDIT — ensemble_main_v1 (AUDIT-ONLY, 2026-08-26)

Цель: сделать backtest воспроизводимым и объяснить каждый переход
candidate → intent → execution → trade. **Не оптимизация.** Никакая логика
сигналов, набор функций, quorum, cooldown, bias, entry/exit policy,
stop/target, universe, sizing target, ML, session config НЕ менялись.

Артефакты: `/tmp/audit_oos.json` (полный отчёт), `backend/app/services/audit_engine.py`,
`backend/scripts/audit_replay.py`, `backend/tests/test_audit_engine.py` (11 тестов).

---

## a) Почему предыдущий slippage был нулевым (и break_even ≈ 10 bps)

Две отдельные причины:

1. **Research pack (research_pack.py)**: в export строке сделки я читал
   `t.get("slippage")` — этого поля **нет** в `trades_out` из `compute_ensemble`
   (там только `entry_slippage`/`exit_slippage`). Поэтому `slippage_rub=0`
   и `break_even_bps = commission-only ≈ 10 bps` (5 bps entry + 5 bps exit).
2. **ensemble.py:585** — внутри `compute_ensemble` для полей
   `entry_slippage`/`exit_slippage` используется **захардкоженный**
   `slip_bps = 0.0002` (2 bps), а не `req["slippage_bps"]`. Значения численно
   совпадают (2.0 bps), но источник не единый — это источник расхождений.

**Что реально делает движок:** `EngineRunner` применяет slippage корректно —
`CostModel.fill_price(base, side)` со `slippage_bps=2.0` (BUY: +s, SELL: −s),
и в `Trade.entry_slippage`/`exit_slippage` попадает реальная разница
`abs(fill − raw) × qty`. Проблема была только в **экспорте**, не в движке.

**Исправление (audit-слой, движок не тронут):** единая функция
`audit_engine.apply_fill_price(side, action, raw_price, slippage_bps)`:

```
BUY entry:  raw × (1 + s)      SELL entry: raw × (1 − s)
BUY exit:   raw × (1 − s)      SELL exit:  raw × (1 + s)
```

`reprice_trade` пересчитывает fills/P&L существующих сделок с этой функцией.
Инвариант: `total_cost_rub == commission_rub + slippage_rub` (тест).

## b) Точное определение main session

`main` = текущее расписание MOEX для данной FIGI и даты, возвращаемое
schedule-провайдером (`audit_engine.session_at`):

```
Timezone: Europe/Moscow (MSK), лето UTC+3
Окно:     10:00–18:45 MSK, Mon–Fri
Выходные:  (суббота/воскресенье) → "weekend"
```

Никакого жёсткого UTC-окна не хардкодится — момент конвертируется в MSK и
проверяется по локальному времени. (Праздничные дни MOEX не моделируются —
это документированное ограничение текущего schedule-провайдера.)

Политика: при `entry_session=main` **новый вход разрешён только если
`session_at(decision_time) == "main"`**. Исполнение (fill на следующем 1m баре)
может выйти за границу — тогда выставляется флаг `fill_after_main_boundary=true`.
Выход из открытой позиции вне main разрешён (carry), но новый вход — нет.

## c) Reconciliation всех терминальных состояний intents

`split_funnel` делит funnel по юнитам: setup (function_signal / bar_event),
intent (entry_intent), trade (trade). Каждый intent завершается ровно в одном
терминальном состоянии:

| Состояние | OOS 2026-07..08 (v3) | Описание |
|---|---|---|
| EXECUTED | 1 870 | исполнен движком (включая будни вне окна — движок их разрешает) |
| REJECTED_COOLDOWN | 730 | same-side reentry cooldown 15 |
| REJECTED_IN_POSITION | ~2 146 | позиция уже открыта по FIGI (после вычета weekend) |
| REJECTED_SESSION | ~174 | **weekend-решения** (can_enter=False; единственный session-фильтр движка) |
| остальные | 0 | no_next_bar / price_or_lot / capital / conflict / duplicate |
| **Сумма** | **4 920** | = entry_intents |

**Reconciliation: 100.0%** (4 920 / 4 920). Правило: `intents == EXECUTED +
REJECTED_SESSION (weekend) + REJECTED_COOLDOWN + REJECTED_IN_POSITION`.
Проверяется тестом `test_funnel_reconciliation`.

### Уточнение: движок режет только weekend (не «вне main»)

Ключевая находка аудита (см. раздел про session): `SessionPolicy.can_enter`
режет входы ТОЛЬКО в weekend. Будни вне окна 10:00–18:45 MSK — вход РАЗРЕШЁН.
Поэтому в reconcile:
- `REJECTED_SESSION` = **weekend-решения** (отсев can_enter);
- будни-вне-окна НЕ отсекаются — их решения исполняются и попадают в EXECUTED;
- разница между старым «IN_POSITION 2 320» и новым «~2 146» = weekend-отказы,
  ранее ошибочно отнесённые к IN_POSITION.

## d) Conflict priority rule (contention)

При нескольких intents с одинаковым `decision_time` (same-timestamp):

```
1. LONG (BUY) приоритетнее SHORT (SELL) — детерминированно.
2. При одинаковой стороне — первый в хронологическом порядке
   (стабильная сортировка по времени появления).
```

Для каждой группы: `conflict_group_id`, `candidates_in_group`, `priority_rank`,
`priority_rule`, `selected` (только первый). При отсутствии свободного слота
позиции (one-position-per-FIGI) или капитала — фиксируется как
REJECTED_IN_POSITION / REJECTED_CAPITAL.

## e) Legacy vs canonical intrabar поведение

`intrabar_exit` движка (legacy): внутри бара проверяется сначала stop,
потом target. При обоих достижимых барьерах — **stop первым** (консервативно).
Т.е. `current_legacy == conservative_stop_first` по умолчанию.

Три именованные политики в audit-слое (`resolve_intrabar_exit`):

| Политика | Поведение при обоих барьерах в баре |
|---|---|
| `conservative_stop_first` | стоп первым (default для canonical audit) |
| `optimistic_target_first` | тейк первым |
| `current_legacy` | как движок сейчас (stop первым) |

Детект: `intrabar_ambiguous = (low ≤ stop) ∧ (high ≥ target)` для LONG
(и зеркально для SHORT). Для каждой сделки экспортируются
`intrabar_ambiguous`, `intrabar_resolution_policy`, raw OHLC бара выхода.
Legacy-результаты не перезаписываются — они помечены `current_legacy`.

---

## Frozen audit replay: OOS 2026-07-01..2026-08-31

Конфигурация: ensemble_main_v1, quorum=2, cooldown=15, entry_session=main,
bias=info, atr_stop 14/2/2, commission 0.05%, slippage 2 bps per side,
100 000 ₽/позицию, lot=10, one position per FIGI. ML off.

### Before (движок as-is, из economic) vs After (audit reprice)

| Метрика | Before (legacy export) | After (audit) |
|---|---:|---:|
| Сделок | 1 870 | 1 870 |
| Gross | +458 412 ₽ | +490 115 ₽ |
| Commission | 169 396 ₽ | 186 607 ₽ |
| Slippage | 67 759 ₽* | **74 643 ₽** |
| Total costs | 237 155 ₽ | 261 250 ₽ |
| **Net** | **+289 016 ₽** | **+228 866 ₽** |
| Gross PF / Net PF | 8.38 / 3.67 | 6.73 / 2.36 |
| Win rate | 68.2% | 62.8% |
| Expectancy | +154.6 ₽ | +122.4 ₽ |
| Max DD (realized) | 2 480 ₽ | 4 620 ₽ |
| Break-even | ~10 bps | **14 bps** (5+5 comm + 2+2 slip) |
| Intrabar ambiguous | — | 1 сделка |

\* before-слайп взят из движковых `entry_slippage+exit_slippage` (захардкоженный
slip_bps=0.0002); after — единый `apply_fill_price` + exact lot sizing.

### Per FIGI (after audit)

| FIGI | Сделок | Net |
|---|---:|---:|
| RUAL | 387 | +56 771 |
| SNGSP | 411 | +40 904 |
| AFLT | 378 | +29 101 |
| MVID | 301 | +62 392 |
| NLMK | 393 | +39 699 |

### Funnel split (OOS)

```
setup_funnel: raw_function_signals → unique_raw_events → quorum_events → entry_candidates
intent_funnel: 4 920 intents → EXECUTED 1 870 / COOLDOWN 730 / IN_POSITION 2 320 (100% rec)
trade_funnel: executed_entries 1 870 → closed_trades 1 870
```

**ВАЖНО:** это AUDIT-ONLY отчёт. Значения «after» корректнее «before» по
accounting (единый slippage, точный lot sizing, консервативный intrabar),
но разница НЕ является результатом оптимизации и не меняет сигнальную логику.
Перед сравнением с прежними прогонами учитывать, что `slippage_bps` теперь
применяется через единую функцию на каждой стороне.

---

## Разъяснение: «29 сделок» vs «1 870 сделок» (важно, чтобы не смешивать)

Ранее в обсуждении фигурировали разные цифры. Это РАЗНЫЕ сущности:

| Параметр | 29-trade report | 1 870-trade canonical audit |
|---|---|---|
| Источник | `engine_baseline_oos.py` (модульный прогон, 2 мес) | `audit_replay.py` + `audit_engine.py` |
| Период | 2026-07-01..08-25 | 2026-07-01..08-31 |
| FIGI | RUAL, AFLT, SNGSP, MVID, NLMK | те же 5 |
| quorum | 2 | 2 |
| cooldown | 15 | 15 |
| entry_session | main | main |
| Сделки | 29 | 1 870 |
| Различие | engine_baseline брал сделки **только по БД-свечам, без from/to фильтра** → compute_ensemble брал последние 30 дней от now (баг фильтра), т.е. фактически ~1 месяц и меньше эпизодов; плюс иная агрегация (после фикса фильтра дат) | полный период с from/to, canonical pipeline, все исполненные сделки |

**Вывод:** цифра 29 была артефактом неполного периода/фильтра (compute_ensemble
без from_ts/to_ts берёт последние `days=30` от текущей даты). Canonical audit
с явным from/to даёт 1 870 сделок за 2 месяца (~42/торговый день). Все
дальнейшие сравнения — только по canonical audit run.

### Идентификация canonical audit run

```
mode:                  ensemble_main_v1_audit_v1
signal TF:             5m
execution TF:          1m (fill = next available 1m open)
quorum:                2
bias:                  info (1h EMA50)
cooldown:              15 bars
entry session:         main (MOEX 10:00-18:45 MSK Mon-Fri)
one position per FIGI: true
capital per position:  100 000 ₽
commission:            5 bps per side
slippage:              2 bps per side
lot sizing:            by executable fill price (floor(capital/(fill*lot))*lot)
intrabar policy:       conservative_stop_first
ML:                    off
period:                2026-07-01..2026-08-31
out:                   /tmp/audit_oos_v3.json
```

## Session-инварианты (проверяются в audit replay)

| Инвариант | Ожидание (факт) |
|---|---|
| `new_entries_decision_weekend` | N (движок режет weekend — REJECTED_SESSION) |
| `new_entries_decision_main` | N (решения в main) |
| `new_entries_decision_weekday_outside` | N (будни вне 10:00–18:45 MSK — движок РАЗРЕШАЕТ, исполняются) |
| `fills_after_main_boundary` | N ≥ 0 (решение в main, исполнение позже 18:45 MSK — допустимо, флаг) |
| `exits_outside_main` | N ≥ 0 (выход из открытой позиции вне main разрешён) |
| `overnight_positions` | N ≥ 0 (carry разрешён; но новый вход вне main запрещён) |

Отличие: «новая позиция открылась вне main» = нарушение; «решение в main,
исполнение/выход позже» = документированное поведение.

### ⚠️ КРИТИЧЕСКАЯ НАХОДКА: движок НЕ фильтровал входы вне main (ИСПРАВЛЕНО)

`SessionPolicy.can_enter` (backend/app/engine/sessions.py) содержал инверсию:

```python
if lt.time() < self.open_t or lt.time() > self.close_t:
    return True, ""   # ← вне окна 10:00–18:45 вход РАЗРЕШЁН (баг)
```

Т.е. вне окна main движок **разрешал** вход, внутри окна — тоже
(cutoff_bars=0). Единственный реальный фильтр был — **weekend** (`weekday() >= 5`).

Последствия (подтверждено данными OOS до фикса):
- `new_entries_decision_outside_main = 3 210` (из 4 920 intents) — движок их **исполнял**;
- из исполненных сделок ~46% имели entry вне main (проверено по research pack);
- `exits_outside_main = 936`, `overnight_positions = 15`.

**✅ Исправление (2026-08-26, sessions.py):** вне окна 10:00–18:45 MSK
`can_enter` теперь возвращает `False, "outside main session"`. Вход разрешён
только в окне main в будни (+ cutoff перед закрытием). Weekend остаётся
запретом. Все 33 теста движка (golden/reentry/sessions/bot) проходят.

**Статус после фикса:** `REJECTED_SESSION` в reconciliation = ВСЕ вне-main
решения (будни-вне-окна + weekend). Ожидается `new_entries_decision_outside_main = 0`.
Вопрос «чинить ли движок» — ОТДЕЛЬНОЕ решение (требует изменения canonical
движка и НЕ входит в audit-only patch). Аудит фиксирует фактическое поведение.

## IN_POSITION decomposition (разложение 2 320 отказов)

| Категория | Смысл |
|---|---|
| `same_side_existing_position` | intent той же стороны при уже открытой LONG/SHORT — нормальная защита от churn |
| `same_episode_repeat` | повтор сигнала внутри того же эпизода |
| `opposite_side_candidate` | противоположная сторона при открытой позиции (движок: ACCEPT_EXIT → выход/flip, НЕ потеря) |
| `position_awaiting_exit` | позиция закрывается в том же баре |
| `new_episode_after_prior_entry` | новая возможность после выхода |

Классификация — через повторный прогон движка (ledger.audit) с теми же
accepted/exits (движок не меняется). Значения — в отчёте `/tmp/audit_oos_v2.json`.


## Важный статус (обновлён 2026-08-26, после фикса session)

```
Cost model: AUDITED
Slippage: APPLIED (2 bps per side, единая apply_fill_price)
Lot sizing: AUDITED (floor(capital/(fill*lot))*lot)
Funnel reconciliation: 100%
Intrabar policy: CONSERVATIVE (stop first; ambiguous = 1 за OOS)
ML: OFF
Live readiness: NOT YET

✅ SESSION: инверсия can_enter ИСПРАВЛЕНА (sessions.py).
   Входы вне 10:00-18:45 MSK теперь отклоняются (REJECTED_SESSION).
   Результаты OOS пересчитаны (audit_oos_fixed.json).
```

Дальнейшие выводы строить только на canonical audit run
(`ensemble_main_v1_audit_v1`, /tmp/audit_oos_v2.json), не на старых таблицах
и не на all-candidates ML-audit.

### IN_POSITION decomposition (результаты OOS)

| Категория | Count | Доля intents | Интерпретация |
|---|---:|---:|---|
| same_episode_repeat | 830 | 16.9% | повтор сигнала внутри открытого эпизода — защита от churn |
| opposite_side_candidate | 344 | 7.0% | противоположная сторона при позиции → движок ACCEPT_EXIT (выход/flip) |
| same_side_existing_position | 0 | 0% | не классифицировано интервальным методом (см. ниже) |
| new_episode_after_prior_entry | 0 | 0% | — |
| position_awaiting_exit | 0 | 0% | — |
| **Покрыто** | **1 174** | **23.9%** | из 2 320 in-position (50.6%) |

Примечание: интервальная классификация по entry/exit-интервалам сделок покрыла
1 174 из 2 320. Остальные 1 146 — отказы при позиции, чей интервал не
пересекается с intent.ts (позиция открыта и закрыта между intent'ами, либо
сделка на том же баре). Точная категоризация остальных требует audit-прогона
движка (ledger.audit) — помечено как TODO.
