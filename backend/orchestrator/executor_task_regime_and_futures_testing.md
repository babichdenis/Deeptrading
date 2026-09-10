# executor_task_regime_and_futures_testing.md — PUT ALL ON TESTING (2026-08-29)

**Выдал:** My3 (research lead), 2026-08-29
**Исполнитель:** DeepSeek executor (`.3`)
**Тип:** комбинированный: SHADOW/READ-ONLY (P0-P1) + DATA_PREP (P1) + RESEARCH (P2). НЕ меняет EngineRunner/baseline.
**Статус:** к исполнению, в порядке приоритета.
**Мотивация:** конвейер гипотез на тестирование: подтвердить/опровергнуть макро-режимы и режим-переключение (H-064),
разблокировать futures (H-063), починить дефект DD (H-059). Все тесты — на существующих/открытых данных.

## P0-1. Починить max_dd в H-059 (дефект, найден My3)
- В `backend/reports/h059_monte_carlo.{json,md}` max_dd посчитан как ПИК кумулятивной прибыли (11240 ₽),
  а не просадка. Пересчитать peak-to-trough DD (июль: ~134 ₽) по тем же сериям; обновить перцентили и rank.
  Вывод H-059 («результаты типичны») НЕ меняется.

## P0-2. H-058 на расширенном окне 2026-01-01..2026-07-31 (подтверждение макро-режимов)
- Перенести permutation-тесты H-058 (метод из `scripts`/прошлой задачи: N_PERM=5000, seed=42) на всё первое полугодие 2026:
  (a) макро-режимы: GOLD down vs up, IMOEX up vs down, USDRUB down vs up, BRENT (метод B/ADX, как macro_regime_attribution_v2);
  (b) волатильность INSIGHT-002: high-vol vs low-vol (ATR>rolling median 20д) — значимость разницы net/trade.
- Деливерабл: `backend/reports/h058_extended_2026h1.{json,md}`. Статус каждого теста: significant / not_significant / insufficient_n.

## P1-3. H-064 пред-тест: mean-reversion в low-vol (SHADOW/read-only)
- Гипотеза: в низковолатильном/боковом режиме MR-сигналы (перепроданность) дают прибыль, тогда как momentum — нет.
- На July (517 сделок, `entry_quality.csv` + бары decision_ts из research_pack/5m):
  - пометить режим волатильности (ATR5m/20d-median, как INSIGHT-002);
  - посчитать на decision_ts MR-признаки: Stoch(14,3) K<20, RSI(14)<30, расстояние до VWAP; 
  - сравнить net/trade в бакетах (low-vol, MR-сигнал есть/нет) — тест H-058 permutation.
- Деливерабл: `backend/reports/h064_mr_lowvol_telemetry.{json,md}`.
- ОГРАНИЧЕНИЕ: это ДИАГНОСТИКА, не вход-правило; не менять стратегию.

## P1-4. H-063 DATA_PREP: futures 5m + OI история (разблокирует futures lead-lag)
- Скачать через MOEX ISS API (бесплатно, как BR/GOLD уже скачаны): 5m OHLCV за 2026-01-01..2026-07-31 для
  RUAL, SNGP, AFLT, MVID, NLMK (одиночные фьючерсы) + IMOEXF (вечный индексный) + RTS. Таймзона MSK, выровнять с барами.
- Если есть — OI история (колонка open positions) за тот же период.
- Деливерабл: `data/futures_5m_*.csv` + manifest (как BR/Gold manifest). Отчёт: `backend/reports/futures_dataprep_{json,md}`.

## P2-5. Attribution новых индикаторов (H-049/H-050/H-052/H-054) — research, после P0-P1
- На July research_pack/5m: добавить как кандидаты-признаки MFI(14)/OBV/CMF(20), Stoch(14,3)/StochRSI, Squeeze Momentum,
  ADX(14)/Aroon(25) — по одному признаку, attribution как в EXP-003 (pair/vote, CI95). Не менять ensemble.
- Деливерабл: `backend/reports/indicator_attribution_v1.{json,md}`.

## Тесты/инварианты
- Всё point-in-time (признаки <= decision_ts), без look-ahead. Движок, config_hash 1c7f75dc44c2aa67, сделки — НЕ трогать.
- Итог: `python -m orchestrator submit "@backend/reports/regime_futures_summary.md"` (сводный вердикт по P0-P2).

## Жёсткие запреты
- SHADOW/read-only + DATA_PREP; НЕ запускать primary-эксперименты; НЕ менять baseline/параметры; без графиков.
- ВАЖНО: backend/.venv/bin/python битый (dangling symlink). Использовать рабочий интерпретатор с numpy/pandas
  (или починить venv), как в прошлой задаче H-058/H-059.
