# Brent / Gold / USD-RUB — data sources (point-in-time, 1m/5m)

**Задача:** H-036 Фаза 1 (макро-атрибуция 5-FIGI сделок по Brent/Gold/USD-RUB).
Требования: 1m/5m, точные timestamps (MSK), point-in-time (без lookahead), покрытие July 2026.

## Источники (оценка)

| Источник | Free/Paid | Granularity | Historical depth | Point-in-time | Применимость для MOEX-атрибуции |
|---|---|---|---|---|---|
| **T-Invest API (MOEX фьючерсы BR, GOLD, Si/USDRUB)** | free (с токеном) | 1m/5m | полная история фьючерсов | **Да** (биржевые timestamps) | **Рекомендуется** — самый надёжный; BR = Brent-фьючерс, GOLD = золото-фьючерс, Si = USD-RUB |
| MOEX own history-data | зависит от контракта | 1m | полная | Да | Альтернатива T-Invest (тот же биржевой источник) |
| Alpha Vantage | free (key) / paid | 1m (5m) | ~30 дней intraday (free) | Умеренно (задержки возможны) | Только если T-Invest недоступен; free-лимит мал |
| Twelve Data | free/paid | 1m/5m | free ~8 отсчётов/мин | Умеренно | Частично |
| Investing.com | free (ручная) | 1m | глубокая | Задержка (CFD) | НЕ point-in-time строго |
| Yahoo Finance | free | 1m/5m | delayed | **Нет** (задержка) | Исключён (не point-in-time) |

## Рекомендация
**T-Invest API** — доступен в проекте (токен в `.env`, библиотека `t-tech-investments`).
MOEX-фьючерсы:
- `BR` (Brent) — figi бэквордного контракта, актуальный месяц;
- `GOLD` (золото) — фьючерс;
- `Si` (USD-RUB) — валютный фьючерс.

Это даёт биржевые point-in-time 1m/5m свечи с корректными timestamps (MSK), что критично
для атрибуции сделок trades.csv (July 2026).

## Статус
- **DATA_PARTIAL** до попытки выгрузки через T-Invest API.
- Если выгрузка фьючерсов недоступна (маржинальные/неликвидные) — **DATA_UNAVAILABLE**,
  fallback: IMOEX-only proxy (уже сделано в sector_correlation_shadow).

## Действие
Попробовать T-Invest `getCandles` для BR/GOLD/Si за July 2026 (и 2026-окна).
При успехе — сохранить `data/brent_5m.csv`, `data/gold_5m.csv`, `data/usdrub_5m.csv`.
