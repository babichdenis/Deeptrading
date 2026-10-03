# Regime v2 — Hysteresis: flicker / lag (Stage C.5, read-only)

- Dataset: T, SBER, LKOH, ROSN, NVTK, SMLT, 93 дней, 2026-07-01T08:00:00+00:00 .. 2026-10-02T08:00:00+00:00
- TFs: 5m, 1h; v2 W=12; sticky structure 0.4/0.25; lag-окно 24 баров
- DSN: 192.168.1.7:5432/deeptrading (read-only); provider остаётся legacy, runtime не менялся

## 5m

| вариант | transitions/1000 | singleton share | mean run | median run | lag p50 | lag p90 | принято в окне |
|---|---|---|---|---|---|---|---|
| legacy | 112.0 | 39.9% | 8.9 | 2.0 | — | — | 0.0% |
| stateless | 273.6 | 44.9% | 3.7 | 2.0 | — | — | 0.0% |
| mild | 75.8 | 0.0% | 13.2 | 7.0 | 1.0 | 13.0 | 78.0% |
| default | 73.1 | 0.0% | 13.7 | 7.0 | 1.0 | 13.0 | 77.8% |
| strong | 47.8 | 0.0% | 20.9 | 12.0 | 0.0 | 12.0 | 68.3% |

| label | stateless | default hyst |
|---|---|---|
| TREND_UP | 0.0% | 0.0% |
| TREND_DOWN | 0.0% | 0.0% |
| RANGE | 51.5% | 67.7% |
| HIGH_VOLATILITY | 10.0% | 6.2% |
| NEUTRAL | 38.5% | 26.1% |

## 1h

| вариант | transitions/1000 | singleton share | mean run | median run | lag p50 | lag p90 | принято в окне |
|---|---|---|---|---|---|---|---|
| legacy | 272.1 | 47.9% | 3.7 | 2.0 | — | — | 0.0% |
| stateless | 340.9 | 48.7% | 2.9 | 2.0 | — | — | 0.0% |
| mild | 76.0 | 0.0% | 13.1 | 8.0 | 1.0 | 3.0 | 55.3% |
| default | 73.1 | 0.0% | 13.6 | 9.0 | 0.0 | 3.0 | 54.9% |
| strong | 51.2 | 0.0% | 19.3 | 13.0 | 0.0 | 4.0 | 48.0% |

| label | stateless | default hyst |
|---|---|---|
| TREND_UP | 12.5% | 18.7% |
| TREND_DOWN | 14.9% | 21.0% |
| RANGE | 38.3% | 54.0% |
| HIGH_VOLATILITY | 9.6% | 5.3% |
| NEUTRAL | 24.7% | 0.9% |

## Ограничения

- Provider default остаётся `legacy`; runtime/гейты/UI не менялись.
- Метрики по всем барам (включая weekend); лаг — только для смен, принятых в окне 24 баров; непринятые считаются отдельно.
- `stateless` — те же измерения v2 без состояния (не legacy).
