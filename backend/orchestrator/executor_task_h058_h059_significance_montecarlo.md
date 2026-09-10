# executor_task_h058_h059_significance_montecarlo.md

**Выдал:** My3 (research lead), 2026-08-28
**Исполнитель:** DeepSeek executor (`.3`)
**Тип задачи:** SHADOW_RESEARCH / READ-ONLY (телеметрия; НЕ меняет EngineRunner/стратегию)
**Статус:** к исполнению.
**Связанные гипотезы:** WORLD-H-058 (Rule Significance Testing), WORLD-H-059 (Monte Carlo).
**Мотивация:** 5 подряд REJECTED primary (EXP-002b, EXP-003a, B4-run deep, B4-run medium, session-expansion).
Shadow-эффекты (B4 deep 38.7 vs shallow 4.0; quorum3 25.4 vs quorum2 20.4; macro-regime разрывы) не воспроизводятся
на primary-прогонах. Нужен статистический фильтр значимости факторных разрывов ДО запуска primary (H-058)
и оценка устойчивости результатов (H-059). Это сэкономит OOS-окна.

## Цель 1: H-058 — Rule Significance Testing (permutation/bootstrap)

Проверить, значимы ли наблюдаемые разрывы net/trade между группами, на СУЩЕСТВУЮЩИХ per-trade данных
(случайная перестановка меток групп; p-value). НЕ строить новых входов, НЕ менять движок.

### 1a. B4 pullback: deep vs shallow
- Источник: `backend/reports/5b44f3b383df/entry_quality.csv` (517 сделок July 2026; колонки `net`, `pullback_bps`).
- Пороги групп из entry_quality.json: shallow = pullback_bps < Q1=42.66 bps; deep = pullback_bps > Q2=82.02 bps
  (по терцилям n≈173/173/171). Проверить n и пороги по данным.
- Тест: разница средних net (deep − shallow). Нулевая гипотеза: метки deep/shallow случайны.
  Permutation: перемешать метки групп между сделками, 5000 итераций (seed=42), пересчитать разницу средних.
  p-value = доля перестановок с |diff_perm| >= |diff_actual| (two-sided) и P(diff_perm >= diff_actual) (one-sided).
- Заодно повторить для фильтра require_deep (deep vs non-deep = medium+shallow) — это воспроизводит S1 B4-run.

### 1b. B3 quorum: quorum>=3 vs quorum==2
- Источник: `backend/reports/5b44f3b383df/vote_attribution_dataset_v1/intent_vote_features.csv`
  (per-intent; колонки `quorum_count`, `net`).
- Группы: quorum_count>=3 vs quorum_count==2 (по trade_id, только исполненные intents, net != NaN).
- Тот же permutation-тест разницы средних net.

### 1c. H-036 macro regimes (v2, Brent/Gold/USD-RUB теперь доступны)
- Использовать per-trade разметку режимов из прогона `macro_regime_attribution_v2` (или повторно вывести
  метки IMOEX/GOLD/USDRUB/BRENT regime на trades.csv тем же методом — скрипт `scripts/macro_regime_attribution_v2.py`).
- Тесты разниц средних net/trade: IMOEX up vs down; GOLD down vs up; USDRUB down vs up.
- ВАЖНО: это множественное тестирование (4 фактора × пары) — вывести число тестов и поправку Бонферрони.

### 1d. (Опционально) function presence: fn_X==1 vs fn_X==0 по intent_vote_features.csv
- Для каждой из 7 функций — permutation-тест. Укрепит решение EXP-003a/EXP-003b (RSI/VWAP).

**Формат вывода (на каждый тест):** n1, n2, mean1, mean2, diff, p_two_sided, p_one_sided,
95% CI нулевого распределения. Плюс колонка «статус»: significant (p<0.05) / not_significant /
insufficient_n (min(n)<30).

## Цель 2: H-059 — Monte Carlo устойчивость

- Источник: `backend/reports/5b44f3b383df/trades.csv` (517 сделок July; колонка `net_rub`).
  Если для других окон (EXP-003a 2025-01..08, EXP-002b 2025-08..12, B4-run 2026-05..07) есть trades.csv в их
  run-директориях — прогнать Monte Carlo и для них (необязательно; по одному JSON на окно).
- Метод: 10 000 симуляций, sampling with replacement из ряда net_rub; для каждой симуляции: сумма (net),
  max DD (по кумулятивной сумме), PF (gross_profit/|gross_loss|).
- Вывести перцентили 5/50/95 для net, PF, max DD; и положение actual значений (percentile rank).
- Интерпретация: если actual net в 50-м перцентиле — типичный результат; в 95-м — likely lucky.

## Деливераблы
- `backend/reports/h058_significance_testing.json` + `h058_significance_testing.md` (таблицы, verdict per фактор).
- `backend/reports/h059_monte_carlo.json` + `h059_monte_carlo.md`.
- НИКАКИХ графиков (рисовать ничего не надо). Только числа/таблицы.

## Код (заготовка, адаптировать под фактические пути)

```python
import numpy as np, pandas as pd, json, os

R = "backend/reports"
N_PERM, SEED = 5000, 42
rng = np.random.default_rng(SEED)

def perm_test(nets, group):
    """nets: np.array; group: np.array(0/1). Разница средних (grp1-grp0) + p. """
    g = group.astype(int)
    d_actual = nets[g == 1].mean() - nets[g == 0].mean()
    d_perm = np.empty(N_PERM)
    for i in range(N_PERM):
        gi = rng.permutation(g)
        d_perm[i] = nets[gi == 1].mean() - nets[gi == 0].mean()
    p_two = (np.abs(d_perm) >= np.abs(d_actual)).mean()
    p_one = (d_perm >= d_actual).mean()
    lo, hi = np.percentile(d_perm, 2.5), np.percentile(d_perm, 97.5)
    return dict(n0=int((g == 0).sum()), n1=int((g == 1).sum()),
                mean0=float(nets[g == 0].mean()), mean1=float(nets[g == 1].mean()),
                diff=float(d_actual), p_two_sided=float(p_two), p_one_sided=float(p_one),
                ci95=[float(lo), float(hi)])

# 1a B4 (entry_quality.csv)
eq = pd.read_csv(f"{R}/5b44f3b383df/entry_quality.csv")
q1, q2 = 42.66, 82.02
deep = (eq.pullback_bps > q2); shal = (eq.pullback_bps < q1)
sel = deep | shal
res_b4 = perm_test(eq.loc[sel, "net"].values,
                   (deep.loc[sel]).astype(int).values)

# 1b B3 quorum (intent_vote_features.csv)
ivf = pd.read_csv(f"{R}/5b44f3b383df/vote_attribution_dataset_v1/intent_vote_features.csv")
ivf = ivf[ivf.net.notna()]
q3 = ivf.quorum_count >= 3
res_b3 = perm_test(ivf["net"].values, q3.astype(int).values)

# 2 H-059 Monte Carlo (trades.csv)
tr = pd.read_csv(f"{R}/5b44f3b383df/trades.csv")
nets = tr.net_rub.values
N_MC = 10000
mc = np.empty((N_MC, 3))  # net, dd, pf
for i in range(N_MC):
    s = rng.choice(nets, size=nets.size, replace=True)
    mc[i, 0] = s.sum()
    cs = np.cumsum(s)
    mc[i, 1] = (cs.max() - cs).max()
    pos = s[s > 0].sum(); neg = abs(s[s < 0].sum())
    mc[i, 2] = pos / neg if neg > 0 else np.inf
# вывод перцентилей + ранга actual
```

## Тесты/инварианты
- Все признаки и net — из существующих артефактов (point-in-time; без новых данных и без look-ahead).
- EngineRunner, config_hash 1c7f75dc44c2aa67, сделки — НЕ трогать.
- Итог: `python -m orchestrator submit "@backend/reports/h058_h059_summary.md"` (сводный вердикт).

## Жёсткие запреты
- SHADOW/read-only; НЕ запускать primary-эксперименты; НЕ менять baseline/параметры.
- Без графиков. Без p-хакинга: фиксированные группы, seed, N; все тесты выводить, включая незначимые.
- ВАЖНО: backend/.venv/bin/python битый (dangling symlink на /usr/local/opt/python@3.11). Использовать
  рабочий интерпретатор с numpy/pandas (system python3 + pip install numpy pandas, или починить venv).
