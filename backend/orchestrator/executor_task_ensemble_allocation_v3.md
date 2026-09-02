# TASK: H-077 v3 (allocation re-test) + ансамбль (Vortex + ADX-фильтр)

**От:** My3 · **Дата:** 2026-08-29 · **Статус:** SHADOW + OOS-prereg (не деплоить без отдельного OOS).

## ЧАСТЬ 1 — H-077 v3: allocation БЕЗ vol-target (сценарий владельца)
Модель как в v1/v2 (`batch_block_E.py`, day-capacity, trades July+H1 n=3481, disjoint, last-closed bar, H-059 MC).
**Сценарий владельца (primary):**
- Fixed-size **10k/сделку** (без vol-target).
- **Cap per name = 25%** капитала → size = min(10k, 0.25×total) на имя (при 50k → 10k; при 30k → 7.5k).
- **Dry powder** резерв 5–10% (idle) для асинхронных сигналов.
- **Vol-target: НЕТ.**
- total 30k и 50k.
**Сравнить с:** baseline fixed (v1), v1 vol-target (net −22..30%). Метрики: taken/missed, net, PF, max_DD, %idle, max_funded_names, opportunity_cost, H-059 MC.
**Ожидание владельца (My3 скорректировал):** net ≈ baseline (возврат тех 22–30%, что срезал v1), НЕ +20–40% к baseline; Capital Utilization +20–40% (5 имён вместо 3 при 30k); DD лучше baseline за счёт cap+dry-powder, но мягче чем −33% v1.
**Чувствительность D (risk-budget):** доп. потолок sum(size_i×ATR_i) ≤ 25% капитала одновременно — на случай, если DD велик.
**Деливерабл:** `reports/batch_block_E3.json` + `.md`.

## ЧАСТЬ 2 — ансамбль: +Vortex (H-072) и +ADX-trend фильтр
**Контекст:** владелец просил «выкинуть 2 из базовых 7». My3 КОРРЕКЦИЯ (по фактам): базу 7 НЕ урезаем —
- rsi_reversal удаление ОТВЕРГНУТО OOS (EXP-003a: net 0.91×S0, coverage −224) → оставить.
- vwap_reclaim (n=4) инертен, но «VWAP removal» заблокирован HARD CONSTRAINT → требует отдельного EXP-003b OOS, не теневого решения.
- Остальные 5 несут edge → оставить.
**Действие (расширение, не урезание):**
- **Добавить H-072 Vortex (VI+ > VI−) как 8-ю сигнальную функцию** (quorum=2 среди 8; HARD CONSTRAINT quorum=3 НЕ нарушен).
- **ADX-trend (ADX14>25) — пре-фильтр входа** (require), НЕ голос в кворуме.
- Прогон на disjoint OOS 2025: S0 = canonical `1c7f75dc44c2aa67` (7 func, quorum2) vs S1 = +Vortex (8 func) vs S2 = +Vortex + ADX-фильтр.
  Критерии (как в EXP-002b): net ≥0.95×S0, max_DD ≤0.80×S0, PF≥S0, win≥S0, coverage ≥95%, concentration ≤50%.
**Скоринг:** вести shadow vote-score telemetry (существующий k=200 / B5b) для анализа; **взвешенное голосование в исполнении ЗАБЛОКИРОВАНО** (HARD CONSTRAINT «не weighted votes») — скоры НЕ использовать как live-веса без отдельного OOS-prereg.
**Деливерабл:** configs + `reports/ensemble_vortex_adx.{json,md}`.

## ЧАСТЬ 3 — прочее
- H-063 OI остаётся заблокированным (candles API); lead-lag НЕТ (фьючерсы синхронны). Без действий.
- Все HARD CONSTRAINTS соблюдены (quorum=2, не weighted, не VWAP-removal без OOS, не sizing-deploy до OOS).
