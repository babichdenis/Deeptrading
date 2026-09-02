# TASK: H-086 NautilusTrader parity spike (engine adoption feasibility)

**От:** My3 · **Дата:** 2026-08-29 · **Статус:** SHADOW / infrastructure spike (НЕ меняет стратегию).
**Решение владельца:** adopt NautilusTrader как движок/платформу (backtest+live+risk), портировать наш ensemble_main_v1, перенять лучшее из движка. НЕ клонировать UI (его нет — engine only).

## ЦЕЛЬ
Доказать **research-to-live parity**: Nautilus-backtest наших 5m CSV должен воспроизвести наш известный baseline.
- Baseline (config_hash 1c7f75dc44c2aa67, July 2026): net **+11189.52 ₽**, PF **3.68**, win **71.37%**, **517 сделок**, net/trade **21.64**.
- Если совпадение по net/PF/DD в пределах ~5% — parity подтверждён, Nautilus становится нашим движком.

## ШАГ 1 — Установка / окружение
- `pip install nautilus_trader` (Rust-wheel, PyPI; Python 3.12–3.14, macOS ARM64 ок). Если в текущем venv проблема — поднять чистый venv.
- Проверить, что собирается и импортируется (`from nautilus_trader...`).

## ШАГ 2 — Порт compute_ensemble как Nautilus Strategy
- Перенести 7 функций (rsi_reversal, macd_hist, vwap_reclaim, breakout, donchian, + Vortex/ADX если в базовом наборе) + quorum=2, cooldown 15, как логику `on_bar` стратегии.
- Сохранить **last-closed bar** методологию (j = bisect_right(ts, dt-5min)-1) и прогрев рядов (без lookahead).
- Сигнал на close 5m → исполнение **next-open 1m** (наш режим): реализовать через Nautilus order/trigger (или submit market/limit at next bar open). Это КЛЮЧЕВОЙ риск parity — зафиксировать точно.

## ШАГ 3 — Данные
- Загрузить наши 5m CSV (RUAL/SNGSP/AFLT/MVID/NLMK) как Nautilus BarData / Parquet. Инструменты = наши FIGI.
- Использовать тот же период (July 2026) и тот же капитал/размер (fixed 10k, как baseline).

## ШАГ 4 — Сравнение
- Прогнать backtest, вытащить equity/сделки, сравнить с baseline (net/PF/win/DD/число сделок).
- Если расхождение >5% по net — диагностировать: (а) fill model (market vs limit, slippage), (б) бар-агрегация/таймстемпы, (в) quorum/округление. Итерировать до parity.

## ШАГ 5 — Что перенять из движка (зафиксировать)
- Risk-engine / position-mgmt (релевантно H-077 allocation, H-082 pyramiding).
- Contingency-ордера OCO (наши выходы).
- Tick/order-book backtest (реалистичнее H-079 T-cost).
- Deterministic time model (укрепляет H-058/H-059).

## Деливераблы
`reports/nautilus_parity.json` + `.md` (совпадение/расхождение, диагностика) + `orchestrator submit`. НЕ деплоить в live (MOEX-адаптер отсутствует).
