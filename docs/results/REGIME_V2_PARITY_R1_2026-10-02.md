# Regime v2 — Parity legacy ↔ v2 (Stage C.4, read-only)

- Dataset: T, SBER, LKOH, ROSN, NVTK, SMLT, 93 дней, 2026-07-01T08:00:00+00:00 .. 2026-10-02T08:00:00+00:00
- TFs: 5m, 1h; v2: W=12, direction_tfs=[3600], c_unknown=0.25; legacy — дефолтные пороги
- DSN: 192.168.1.7:5432/deeptrading (read-only); provider остаётся legacy, runtime не менялся

**Сравнение по строкам:** legacy `RegimeDetector` и `derive_legacy(v2)` на каждом баре.

## 5m

- bars: 120102, agreement: **69.9%**

| label | legacy | v2 |
|---|---|---|
| TREND_UP | 1712 (1.4%) | 0 (0.0%) |
| TREND_DOWN | 1341 (1.1%) | 0 (0.0%) |
| RANGE | 83797 (69.8%) | 108027 (89.9%) |
| HIGH_VOLATILITY | 29797 (24.8%) | 11997 (10.0%) |
| NEUTRAL | 3455 (2.9%) | 78 (0.1%) |

**Матрица legacy → v2 (n, % строки):**

| legacy \ v2 | TREND_UP | TREND_DOWN | RANGE | HIGH_VOLATILITY | NEUTRAL |
|---|---|---|---|---|---|
| TREND_UP | 0 (0%) | 0 (0%) | 1389 (81%) | 323 (19%) | 0 (0%) |
| TREND_DOWN | 0 (0%) | 0 (0%) | 1109 (83%) | 232 (17%) | 0 (0%) |
| RANGE | 0 (0%) | 0 (0%) | 78363 (94%) | 5434 (6%) | 0 (0%) |
| HIGH_VOLATILITY | 0 (0%) | 0 (0%) | 24254 (81%) | 5543 (19%) | 0 (0%) |
| NEUTRAL | 0 (0%) | 0 (0%) | 2912 (84%) | 465 (13%) | 78 (2%) |

**Сессии:**

| session | n | agreement | v2: TREND_UP | TREND_DOWN | RANGE | HIGH_VOL | NEUTRAL |
|---|---|---|---|---|---|---|---|
| clearing | 1581 | 66.4% | 0 | 0 | 1435 | 146 | 0 |
| day | 42769 | 55.7% | 0 | 0 | 38526 | 4165 | 78 |
| evening | 22679 | 85.1% | 0 | 0 | 21331 | 1348 | 0 |
| morning | 13712 | 63.6% | 0 | 0 | 11479 | 2233 | 0 |
| other | 2082 | 86.8% | 0 | 0 | 1899 | 183 | 0 |
| weekend | 37279 | 78.6% | 0 | 0 | 33357 | 3922 | 0 |

**Оси v2:** direction {'FLAT': 120102}; volatility {'UNKNOWN': 78, 'NORMAL': 59136, 'LOW': 30954, 'HIGH': 17937, 'EXTREME': 11997}; structure {'TRANSITION': 52580, 'RANGE': 67522}; confidence {'p10': 0.5679933665008292, 'p50': 0.794594742782624, 'p90': 0.9293502281768872}

**Топ расхождений (legacy -> v2):** HIGH_VOLATILITY -> RANGE: 24254, RANGE -> HIGH_VOLATILITY: 5434, NEUTRAL -> RANGE: 2912, TREND_UP -> RANGE: 1389, TREND_DOWN -> RANGE: 1109, NEUTRAL -> HIGH_VOLATILITY: 465, TREND_UP -> HIGH_VOLATILITY: 323, TREND_DOWN -> HIGH_VOLATILITY: 232

## 1h

- bars: 10685, agreement: **19.3%**

| label | legacy | v2 |
|---|---|---|
| TREND_UP | 712 (6.7%) | 1337 (12.5%) |
| TREND_DOWN | 617 (5.8%) | 1592 (14.9%) |
| RANGE | 5612 (52.5%) | 79 (0.7%) |
| HIGH_VOLATILITY | 2270 (21.2%) | 1026 (9.6%) |
| NEUTRAL | 1474 (13.8%) | 6651 (62.2%) |

**Матрица legacy → v2 (n, % строки):**

| legacy \ v2 | TREND_UP | TREND_DOWN | RANGE | HIGH_VOLATILITY | NEUTRAL |
|---|---|---|---|---|---|
| TREND_UP | 327 (46%) | 4 (1%) | 0 (0%) | 64 (9%) | 317 (45%) |
| TREND_DOWN | 0 (0%) | 241 (39%) | 0 (0%) | 74 (12%) | 302 (49%) |
| RANGE | 580 (10%) | 655 (12%) | 17 (0%) | 276 (5%) | 4084 (73%) |
| HIGH_VOLATILITY | 315 (14%) | 466 (21%) | 1 (0%) | 510 (22%) | 978 (43%) |
| NEUTRAL | 115 (8%) | 226 (15%) | 61 (4%) | 102 (7%) | 970 (66%) |

**Сессии:**

| session | n | agreement | v2: TREND_UP | TREND_DOWN | RANGE | HIGH_VOL | NEUTRAL |
|---|---|---|---|---|---|---|---|
| clearing | 401 | 15.0% | 51 | 66 | 3 | 16 | 265 |
| day | 3609 | 27.0% | 413 | 473 | 18 | 571 | 2134 |
| evening | 1594 | 16.0% | 264 | 288 | 18 | 30 | 994 |
| morning | 1186 | 23.5% | 167 | 130 | 9 | 249 | 631 |
| other | 560 | 12.5% | 100 | 93 | 4 | 4 | 359 |
| weekend | 3335 | 12.8% | 342 | 542 | 27 | 156 | 2268 |

**Оси v2:** direction {'FLAT': 163, 'DOWN': 5258, 'UP': 5264}; volatility {'UNKNOWN': 78, 'NORMAL': 5250, 'HIGH': 1572, 'LOW': 2759, 'EXTREME': 1026}; structure {'TRANSITION': 7292, 'TRENDING': 3328, 'RANGE': 65}; confidence {'p10': 0.5182421227197348, 'p50': 0.7222222222222222, 'p90': 0.8333333333333334}

**Топ расхождений (legacy -> v2):** RANGE -> NEUTRAL: 4084, HIGH_VOLATILITY -> NEUTRAL: 978, RANGE -> TREND_DOWN: 655, RANGE -> TREND_UP: 580, HIGH_VOLATILITY -> TREND_DOWN: 466, TREND_UP -> NEUTRAL: 317, HIGH_VOLATILITY -> TREND_UP: 315, TREND_DOWN -> NEUTRAL: 302, RANGE -> HIGH_VOLATILITY: 276, NEUTRAL -> TREND_DOWN: 226, NEUTRAL -> TREND_UP: 115, NEUTRAL -> HIGH_VOLATILITY: 102

## Ограничения

- Parity диагностическая: provider default остаётся `legacy`; БД/UI/гейты не менялись.
- v2-метка — производная от observation; 5m direction=FLAT по решению владельца, поэтому TREND_* на 5m в v2 не появляются.
- Hysteresis нет: сравнение по-барное; следующий этап — parity каждого префикса.
