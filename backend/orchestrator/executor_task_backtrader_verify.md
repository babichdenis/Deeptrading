# executor_task_backtrader_verify.md — АУДИТ backtrader: валидация + адопция

**Выдал:** My3 (research lead), 2026-08-29
**Исполнитель:** DeepSeek executor (`.54`)
**База:** `docs/research/AUDIT_backtrader.md` · библиотека https://github.com/mementum/backtrader

## Задача (read-only до внедрения; новые индикаторы — в отдельные модули)
1. **Установить backtrader** в рабочий python (`pip install backtrader`; venv бэкенда битый — использовать
   свой рабочий интерпретатор, НЕ трогать `.venv`).
2. **Загрузить наши данные** в backtrader feed: 5m/1m свечи 5 FIGI (из БД/CSV) + `data/gold_5m.csv`
   (переиспользовать `load_factor` из `scripts/macro_regime_attribution_v2.py`).
3. **Вычислить reference-индикаторы backtrader** на тех же данных: EMA(20,50), SMA, RSI(14),
   MACD(12,26,9), ATR(14), ADX/DI+(14), Bollinger(20,2), Donchian, VWAP, Stochastic(14), CCI(20),
   Williams%R(14), Aroon(14), PSAR, Ichimoku, OBV, MFI(14).

### P0 — ВАЛИДАЦИЯ того, что у нас есть (сравнить на наших данных)
- Для EMA/RSI/MACD/ATR: посчитать нашими функциями (`app/services/indicators.py`,
  `app/engine/indicators.py`) и backtrader на одинаковых свечах → таблица max|diff| и корреляции.
  Толеранс: EMA/SMA ~1e-9; RSI/ATR/MACD < 1e-3 (различия в сиде EMA допустимы, зафиксировать).
- Bollinger/Donchian/VWAP: **вынести из сигнальных стратегий** в `app/services/indicators.py` как
  чистые функции и сравнить с backtrader.
- **ADX:** заменить наш кастомный `adx14` (простое окно) на **Wilder ADX** (из backtrader
  DirectionalMove). Пересчитать H-036/H-058 макро-атрибуцию этим ADX → подтвердить, что расхождение
  (см. `gold_regime_audit.md`) уходит. Не удалять старый `adx14` без сравнения.
- Если расхождения > толеранса — зафиксировать баг в нашем коде и исправить.

### P1 — АДОПЦИЯ того, чего нет (взять из backtrader)
- Реализовать как индикатор-функции (или тонкие обёртки над backtrader): **Stochastic, CCI,
  Williams%R, Aroon, PSAR, Ichimoku, OBV, MFI**.
- Запустить H-058-стиль атрибуцию (presence/regime permutation, метод как в `h058_h059_significance.py`)
  на July 2026 (517) и 2026-H1 (3481) для НОВЫХ индикаторов: дают ли сделки с этими сигналами
  больше net/trade? Бонферрони (α≈0.0167).
- Приоритет высокий: Stochastic, CCI, Williams%R, Aroon, PSAR, OBV, MFI.

### P2 — КРОСС-ВАЛИДАЦИЯ ДВИЖКА/МЕТРИК (бонус)
- Прогнать baseline `ensemble_main_v1` (`config_hash=1c7f75dc44c2aa67`) в backtrader на July
  (`5b44f3b383df`) → сравнить net/PF/win%/DD с нашим `runner`. Расхождения >1% → баг движка.
- Добавить Sharpe/SQN (backtrader analyzers) как доп. метрики качества в отчёт.

## Деливераблы
- `backend/reports/backtrader_validation.json` + `.md`: таблица diff'ов индикаторов (P0), список
  расхождений/багов, статус ADX-фикса.
- Новые индикатор-модули (`app/services/indicators_backtrader.py` или расширение `indicators.py`).
- `backend/reports/backtrader_new_indicators_attribution.json`: H-058-атрибуция новых (P1).
- Сводка `backend/reports/backtrader_audit_summary.md` + итог
  `python -m orchestrator submit "@backend/reports/backtrader_audit_summary.md"`.

## Жёсткие запреты
- НЕ менять baseline/стратегию/кворум; НЕ удалять прежние артефакты (оставить для сравнения).
- Новые индикаторы — только как функции + атрибуция; не подключать в торговый сигнал до OOS-доказательства.
- backtrader используется как ЭТАЛОН/кросс-чек, НЕ как замена нашего движка.
