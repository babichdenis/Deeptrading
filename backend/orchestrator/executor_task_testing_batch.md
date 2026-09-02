# TASK: ПАКЕТНОЕ ТЕСТИРОВАНИЕ — H-065..H-077 + висящие P1-3/P1-4/P2-5

**От:** My3 · **Дата:** 2026-08-29 · **Приоритет:** P0/P1 (dispatch)
**Контекст:** Завершён майнинг мировых методов (GitHub, backtrader, Habr, Traders' Tips S&C 2001-2010),
зарегистрировано H-065..H-077 (бэклог 77 методов). Владелец: «поставить всё на тесты». GOLD-аудит уже
RESOLVED (lookahead-баг подтверждён, макро НЕ значимо — макро-гейт НЕ строим). Висят с предыдущей сессии:
P1-3 (MR в low-vol), P1-4 (futures DATA_PREP), P2-5 (атрибуция новых индикаторов).

**ЖЁСТКИЕ ТРЕБОВАНИЯ К КАЖДОМУ ТЕСТУ (учтено в GOLD-аудите):**
1. **Last-closed bar** в макро-атрибуции: `j = bisect_right(ts, dt - 5min) - 1` (НЕ `bisect_right(ts, dt) - 1`).
   Ряд ADX/EMA грузить полностью (прогрев), не только 2026-01..08.
2. Окна disjoint: July 2026 (n=517) + 2026-H1 (остальные). Никакой утечки.
3. Значимость — через `scripts/h058_significance_testing.py` (permutation, regime-grouped vs нет) и
   `scripts/h059_monte_carlo.py` (правильный max_dd = peak-to-trough, уже исправлен в reports).
4. Сырьё индикаторов: сначала сверить наши EMA/RSI/MACD/ATR/Bollinger/Donchian/VWAP против backtrader
   (см. AUDIT_backtrader.md и отдельную задачу backtrader_verify — НЕ дублировать, использовать её вывод).

---

## БЛОК A — REGIME-SWITCHING (H-064), P1, ТОП
Цель: дать H-064 объективные regime-сенсоры (вместо сырого ADX) и проверить MR в low-vol.
- **A1. H-071 Fractal Dimension (Ehlers)** — regime-sensor (тренд vs хаос). Считать FD(N=20) на 5m;
  group: сделки при FD<~1.5 (chop) vs FD>~1.5 (trend). H-058 permutation (grouped vs нет) на July+H1.
- **A2. H-065 LinReg + R² (Barbara Star)** — сила тренда. R²(N=20) > 0.20 как фильтр; H-058 на July+H1
  (сделки при R²>порог vs без). Также проверить R² как замена vol-фильтру INSIGHT-002.
- **A3. H-075 N-ATR / Normalized Vol** — норм. волатильность для классификатора.
- **A4. H-076 Z-Score / BB Z-Test / Disparity** — MR-сигналы в low-vol (боковик). H-058 на July+H1:
  MR-сделки (по Z-Score/Disparity) в low-vol-режиме vs momentum.
- **A5. H-064 сводный** (после A1-A4): momentum-стратегии в high-vol/trend, MR в low-vol/chop;
  раздельные equity-кривые; пороги фиксировать ДО прогона.

## БЛОК B — ОБЪЁМНОЕ СЕМЕЙСТВО (провал H-040/H-043), P1/P2
- **B1. H-066 CSI/CSC cluster sentiment** (Habr 934602) — посвечно-объёмный кластер. Считать CSI,
  bull/bear-cluster; H-058 как новый индикатор-функция (добавить к 8 сигналам как фильтр/подтверждение).
- **B2. H-067 VFI (Volume Flow)**, **B3. H-068 VPCI**, **B4. H-069 VWMA/BuffAverage**,
  **B5. H-070 BMP / Bull-Bear Balance** — каждый H-058 как новый индикатор (July+H1).
  (MFI/OBV из backtrader-аудита = то же семейство, см. БЛОК D.)

## БЛОК C — ИНДИКАТОРНЫЕ ПРОБЕЛЫ (из backtrader-аудита + P2-5), P2
- **C1. H-072 Vortex Indicator** (закрывает H-054 Aroon), **C2. H-073 TDI/TCF/TII/TQI/TTF**,
  **C3. H-074 FRAMA (Ehlers)** — H-058 как новые индикаторы.
- **C4. Stochastic / CCI / Williams%R / Aroon / PSAR / Ichimoku / OBV / MFI** (adopt из backtrader,
  ПЕРЕИСПОЛЬЗОВАТЬ вывод backtrader_verify) — H-058 атрибуция каждого (July+H1).
- **P2-5 (висящая):** атрибуция наших 8 сигналов (rsi_reversal, bollinger_reclaim, ...) по вкладу в
  net/win/DD на disjoint — какие несут edge, какие шум. H-058 per-strategy.

## БЛОК D — FUTURES / DATA_PREP (H-063), P1
- **P1-4 (висящая):** DATA_PREP фьючерсы 5m + OI история 2026 (MOEX ISS: RUAL/SNGP/AFLT/MVID/NLMK +
  IMOEXF/RTS/Si/USDRUBF/BR/GOLD). Затем lead-lag (как B2 для IMOEX) + OI-дельта как позиционирование;
  IMOEXF вместо IMOEX cash. H-058 на July+H1.

## БЛОК E — CAPITAL ALLOCATION (H-077), P1  ← новая задача владельца
Проблема: при сигнале на одном имени нельзя вкладывать все деньги — остальные без капитала при пробое.
- **E1. Симуляция на истории (trades.csv per-name net + ATR/N-ATR):**
  - Baseline: текущий fixed 10k/поз.
  - Rule: per-name cap 25% + vol-target sizing (size ∝ 1/N-ATR, H-075) + dry-powder резерв.
  - Прогнать при total = 30k и 50k (второй вскрывает концентрационную ловушку).
  - Метрики: portfolio net, PF, max DD, % idle capital, max одновременно профинансированных имён,
    opportunity-cost (пропущенные сигналы из-за нехватки капитала).
  - H-059 MC на allocation-аугментированной equity (сравнить с baseline).
- **E2.** Вывод: поднимает ли аллокация net/капиталоёмкость и снижает ли идиосинкразную DD.
  Честно: ожидаем +20-60% капиталоёмкости, не «разы», если капитал не простаивает.

---

## ЧТО УЖЕ В РАБОТЕ (не дублировать, использовать вывод):
- `executor_task_macro_lookahead_recompute.md` — макро пересчёт с last-closed баром (GOLD/IMOEX/USDRUB/BRENT).
- `executor_task_backtrader_verify.md` — валидация наших индикаторов + adopt (P2-5/C4 переиспользуют).
- `executor_task_gold_audit.md` — RESOLVED (методология last-closed bar обязательна для всех выше).

## ФОРМАТ ОТВЕТА
Для каждого блока: таблица {method, n(группы/индикатора), mean net/trade, p-value, significant?,
комментарий-пригодность}. Отдельно: E-блок (allocation) с таблицей симуляций. Итоговый вердикт:
что идёт в primary ensemble, что отбрасываем. Записать в `reports/` + уведомить через orchestrator.
