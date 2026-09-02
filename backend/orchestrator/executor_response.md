# Executor response — H-081 (leverage) — финал блока H-078..081

**От:** DeepSeek executor · **Дата:** 2026-08-29 · **Статус:** SHADOW · HARD CONSTRAINTS соблюдены.

## H-081 — Leverage — VERDICT: работает в риск-бюджете, но ТОЛЬКО SHADOW; L=1.5 — безопасный предел
- Precondition (H-079: size-up +net после T-cost) выполнен.
- Плечо L=1.5/2.0 только на high-vol regime (entry NATR > медиана имени), risk-budget sum(size×NATR) ≤ 25% капитала, пул = базовые 5, T-cost включён, H-059 MC (day-block, seed=42).
- **net (с T-cost):** 30k no_lev 1925 → L1.5 **3261** (+69%) → L2.0 **4881** (+153%); 50k no_lev 3904 → L1.5 **6188** (+59%) → L2.0 **8565** (+119%). DD +35%…+114%. PF 1.87→1.98→2.05 (не деградирует).
- **H-059:** P(margin-call @20% кап.)=0.0; P(DD хуже no-lev): L1.5≈0.34, L2.0≈0.48–0.60.
- **Вывод:** живое плечо НЕ вводить (SHADOW only). Если leveraged-исполнение понадобится — только L=1.5, риск-бюджет 25%, high-vol only, обязательный OOS + учёт financing (в модели не был: позиции минутные, пренебрежимо).
- Deliverables: `reports/h081_leverage.json`, `scripts/h081_leverage.py`.

## Итог блока H-078..081
- **H-078** CLOSED: стационарные пары без значимого MR (H-058 p=1.0) — CLOSED.
- **H-079** DONE: T-cost ~10 bps one-way; size-up high-vol остаётся +net (30k 3651→1925, 50k 7260→3903).
- **H-080** DONE: универсум НЕ расширять (все 7 посчитанных MOEX-имён с отрицательным net; 10 без 1m — data gap).
- **H-081** DONE: плечо в риск-бюджете растягивает net 1.6–2.5×, но остаётся shadow (тонкая база после T-cost, одно окно).
- Блокеров нет: `screening_blockers.md` H-078/H-081 сняты (H-079 шаг 2 выполнен).
