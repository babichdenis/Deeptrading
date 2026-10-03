# Regime v2 — Parity legacy ↔ v2 (Stage C.4, read-only)

- Dataset: T, SBER, LKOH, ROSN, NVTK, SMLT, 93 дней, 2026-07-01T08:00:00+00:00 .. 2026-10-02T08:00:00+00:00
- TFs: 5m, 1h; v2: W=12, direction_tfs=[3600], c_unknown=0.25; legacy — дефолтные пороги
- DSN: 192.168.1.7:5432/deeptrading (read-only); provider остаётся legacy, runtime не менялся

**Сравнение по строкам:** legacy `RegimeDetector` и `derive_legacy(v2)` на каждом баре.

## 5m

- bars: 120102, agreement: **46.4%**

| label | legacy | v2 |
|---|---|---|
| TREND_UP | 1712 (1.4%) | 0 (0.0%) |
| TREND_DOWN | 1341 (1.1%) | 0 (0.0%) |
| RANGE | 83797 (69.8%) | 61894 (51.5%) |
| HIGH_VOLATILITY | 29797 (24.8%) | 11997 (10.0%) |
| NEUTRAL | 3455 (2.9%) | 46211 (38.5%) |

**Матрица legacy → v2 (n, % строки):**

| legacy \ v2 | TREND_UP | TREND_DOWN | RANGE | HIGH_VOLATILITY | NEUTRAL |
|---|---|---|---|---|---|
| TREND_UP | 0 (0%) | 0 (0%) | 382 (22%) | 323 (19%) | 1007 (59%) |
| TREND_DOWN | 0 (0%) | 0 (0%) | 256 (19%) | 232 (17%) | 853 (64%) |
| RANGE | 0 (0%) | 0 (0%) | 48434 (58%) | 5434 (6%) | 29929 (36%) |
| HIGH_VOLATILITY | 0 (0%) | 0 (0%) | 11578 (39%) | 5543 (19%) | 12676 (43%) |
| NEUTRAL | 0 (0%) | 0 (0%) | 1244 (36%) | 465 (13%) | 1746 (51%) |

**Сессии:**

| session | n | agreement | v2: TREND_UP | TREND_DOWN | RANGE | HIGH_VOL | NEUTRAL |
|---|---|---|---|---|---|---|---|
| clearing | 1581 | 39.8% | 0 | 0 | 669 | 146 | 766 |
| day | 42769 | 37.3% | 0 | 0 | 19376 | 4165 | 19228 |
| evening | 22679 | 47.5% | 0 | 0 | 10756 | 1348 | 10575 |
| morning | 13712 | 38.9% | 0 | 0 | 5554 | 2233 | 5925 |
| other | 2082 | 56.8% | 0 | 0 | 1186 | 183 | 713 |
| weekend | 37279 | 58.6% | 0 | 0 | 24353 | 3922 | 9004 |

**Оси v2:** direction {'FLAT': 120102}; volatility {'UNKNOWN': 78, 'NORMAL': 59136, 'LOW': 30954, 'HIGH': 17937, 'EXTREME': 11997}; structure {'TRANSITION': 29094, 'TRENDING': 23486, 'RANGE': 67522}; confidence {'p10': 0.5707018241972502, 'p50': 0.7998020478606772, 'p90': 0.9309936670284902}

**Расшифровка legacy RANGE (чем v2-группы отличаются по measurements):**

| v2 группа | n | доля RANGE | trend p50 | dir_str p50 | ER p50 | range/ATR p50 | RV/ATR p50 | conf p50 |
|---|---|---|---|---|---|---|---|---|
| RANGE | 48434 | 57.8% | 0.136 | 0.000 | 0.111 | 0.740 | 0.615 | 0.832 |
| HIGH_VOLATILITY | 5434 | 6.5% | 0.211 | 0.000 | 0.209 | 1.870 | 0.716 | 0.651 |
| NEUTRAL | 29929 | 35.7% | 0.359 | 0.000 | 0.364 | 0.745 | 0.580 | 0.728 |

**Топ расхождений (legacy -> v2):** RANGE -> NEUTRAL: 29929, HIGH_VOLATILITY -> NEUTRAL: 12676, HIGH_VOLATILITY -> RANGE: 11578, RANGE -> HIGH_VOLATILITY: 5434, NEUTRAL -> RANGE: 1244, TREND_UP -> NEUTRAL: 1007, TREND_DOWN -> NEUTRAL: 853, NEUTRAL -> HIGH_VOLATILITY: 465, TREND_UP -> RANGE: 382, TREND_UP -> HIGH_VOLATILITY: 323, TREND_DOWN -> RANGE: 256, TREND_DOWN -> HIGH_VOLATILITY: 232

## 1h

- bars: 10685, agreement: **39.5%**

| label | legacy | v2 |
|---|---|---|
| TREND_UP | 712 (6.7%) | 1337 (12.5%) |
| TREND_DOWN | 617 (5.8%) | 1592 (14.9%) |
| RANGE | 5612 (52.5%) | 4089 (38.3%) |
| HIGH_VOLATILITY | 2270 (21.2%) | 1026 (9.6%) |
| NEUTRAL | 1474 (13.8%) | 2641 (24.7%) |

**Матрица legacy → v2 (n, % строки):**

| legacy \ v2 | TREND_UP | TREND_DOWN | RANGE | HIGH_VOLATILITY | NEUTRAL |
|---|---|---|---|---|---|
| TREND_UP | 327 (46%) | 4 (1%) | 153 (21%) | 64 (9%) | 164 (23%) |
| TREND_DOWN | 0 (0%) | 241 (39%) | 145 (24%) | 74 (12%) | 157 (25%) |
| RANGE | 580 (10%) | 655 (12%) | 2692 (48%) | 276 (5%) | 1409 (25%) |
| HIGH_VOLATILITY | 315 (14%) | 466 (21%) | 521 (23%) | 510 (22%) | 458 (20%) |
| NEUTRAL | 115 (8%) | 226 (15%) | 578 (39%) | 102 (7%) | 453 (31%) |

**Сессии:**

| session | n | agreement | v2: TREND_UP | TREND_DOWN | RANGE | HIGH_VOL | NEUTRAL |
|---|---|---|---|---|---|---|---|
| clearing | 401 | 28.7% | 51 | 66 | 174 | 16 | 94 |
| day | 3609 | 40.7% | 413 | 473 | 1336 | 571 | 816 |
| evening | 1594 | 31.0% | 264 | 288 | 617 | 30 | 395 |
| morning | 1186 | 39.4% | 167 | 130 | 368 | 249 | 272 |
| other | 560 | 33.8% | 100 | 93 | 185 | 4 | 178 |
| weekend | 3335 | 44.6% | 342 | 542 | 1409 | 156 | 886 |

**Оси v2:** direction {'FLAT': 649, 'DOWN': 5046, 'UP': 4990}; volatility {'UNKNOWN': 78, 'NORMAL': 5250, 'HIGH': 1572, 'LOW': 2759, 'EXTREME': 1026}; structure {'TRANSITION': 2934, 'TRENDING': 3329, 'RANGE': 4422}; confidence {'p10': 0.561645857515766, 'p50': 0.7777777777777777, 'p90': 0.9179322682627671}

**Расшифровка legacy RANGE (чем v2-группы отличаются по measurements):**

| v2 группа | n | доля RANGE | trend p50 | dir_str p50 | ER p50 | range/ATR p50 | RV/ATR p50 | conf p50 |
|---|---|---|---|---|---|---|---|---|
| TREND_UP | 580 | 10.3% | 0.494 | 1.000 | 0.464 | 0.674 | 0.545 | 0.765 |
| TREND_DOWN | 655 | 11.7% | 0.498 | 1.000 | 0.399 | 0.532 | 0.424 | 0.772 |
| RANGE | 2692 | 48.0% | 0.151 | 0.344 | 0.117 | 0.689 | 0.487 | 0.827 |
| HIGH_VOLATILITY | 276 | 4.9% | 0.264 | 0.812 | 0.228 | 2.046 | 0.668 | 0.628 |
| NEUTRAL | 1409 | 25.1% | 0.315 | 0.812 | 0.299 | 0.688 | 0.469 | 0.722 |

**Топ расхождений (legacy -> v2):** RANGE -> NEUTRAL: 1409, RANGE -> TREND_DOWN: 655, RANGE -> TREND_UP: 580, NEUTRAL -> RANGE: 578, HIGH_VOLATILITY -> RANGE: 521, HIGH_VOLATILITY -> TREND_DOWN: 466, HIGH_VOLATILITY -> NEUTRAL: 458, HIGH_VOLATILITY -> TREND_UP: 315, RANGE -> HIGH_VOLATILITY: 276, NEUTRAL -> TREND_DOWN: 226, TREND_UP -> NEUTRAL: 164, TREND_DOWN -> NEUTRAL: 157

## Ограничения

- Parity диагностическая: provider default остаётся `legacy`; БД/UI/гейты не менялись.
- v2-метка — производная от observation; 5m direction=FLAT по решению владельца, поэтому TREND_* на 5m в v2 не появляются.
- Hysteresis нет: сравнение по-барное; следующий этап — parity каждого префикса.
