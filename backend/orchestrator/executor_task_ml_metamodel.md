# TASK: H-057 ML meta-model — per-trade outcome classifier as ENTRY FILTER

**От:** My3 · **Дата:** 2026-08-29 · **Статус:** SHADOW (не деплоить). HARD CONSTRAINTS соблюдены.
**Источник:** запрос владельца + Habr/ITI Capital «Эксперимент: алгоритм прогнозирования индексов» (278023, 2016) — подтверждает ML-слой, но directional accuracy ≠ прибыль; ключ = нейтральная зона (фильтр точности).

## СУТЬ (безопасная форма ML)
НЕ заменяем ансамбль и НЕ делаем weighted votes (BLOCKED). Строим **мета-классификатор поверх наших сигналов**, который предсказывает **вероятность, что сделка будет прибыльной**, и используем его как **ENTRY FILTER** (отбрасывать низко-вероятные + нейтральная зона, как в статье ITI). Это снижает false-positive (precision) — то, что подсветила статья, и что нужно нашему ансамблю.

## ЧАСТЬ 1 — Признаки и метка
**Признаки (на момент сигнала, last-closed bar, БЕЗ lookahead):**
- vote-strength каждой из 7+ функций (rsi_reversal, macd_hist, vwap_reclaim, breakout, donchian, Vortex, ADX-trend) + quorum-флаг;
- индикаторы: RSI, MACD-hist, ATR%, ADX, Vortex, N-ATR (INSIGHT-002 vol-фича);
- regime: FD-фаза (trend/chop), vol-band (high/low);
- macro: GOLD/IMOEX/USDRUB/BRENT (returns).
**Метка:** profitable = (exit_PnL > 0) по нашей логике выхода (forward N баров / next-open). Бинарная + можно ordinal (neg/neutral/pos — как нейтральная зона статьи).

## ЧАСТЬ 2 — Модель и валидация
- Модель: logistic / GBM / LightGBM (если sklearn/lightgbm недоступны в env — поднять venv или fallback на чистый Python logistic; НЕ писать NN без данных).
- **Purged walk-forward (López de Prado):** train на окне W_i, test на W_{i+1}, purge + embargo между окнами. Окна: 2024-H2 / 2025-H1 / 2025-H2 / 2026-H1.
- Метрики: AUC/PR-AUC на OOS, calibration.
- **H-058 gating (главный критерий):** применить ML-фильтр (p<thr отбрасывать) к baseline-сигналам на disjoint OOS → **net/trade должен улучшиться >= 1.15x** vs baseline (иначе ML бесполезен — статья предупреждает про accuracy-иллюзию). Также считать, что filtration НЕ режет слишком сильно coverage (оставить >=50% сделок).

## ЧАСТЬ 3 — Что НЕ делать (HARD)
- НЕ заменять ансамбль; НЕ менять веса голосов (weighted voting BLOCKED);
- НЕ менять sizing (H-077 отдельно);
- НЕ raw next-bar price prediction (макс overfit).

## Ожидаемо
ML-фильтр повышает precision (меньше убыточных входов) при приемлемом coverage → net/trade > baseline. Если нет — H-057 CLOSED как не дающий edge над ансамблем.

## Деливерабл
`reports/h057_ml_filter.json` + `.md` (AUC, порог, net/trade baseline vs filtered, coverage%) + `orchestrator submit`.
