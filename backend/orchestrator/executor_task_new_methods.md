# TASK: H-082 Pyramiding · H-083 HRP · H-084 Microstructure · H-085 Factor IC

**От:** My3 · **Дата:** 2026-08-29 · **Статус:** SHADOW (не деплоить). HARD CONSTRAINTS соблюдены.
**Контекст:** новые методы из Mulvaney (H-082) и HKUDS/Vibe-Trading quantlib (H-083/084/085). Все — research/shadow, с готовыми validation-дизайнами в WORLD_METHODS_BACKLOG.md. Данные: 5m OHLCV 5 RU-тикеров (RUAL/SNGP/AFLT/MVID/NLMK), OOS 2025-H1/2025-H2/2026-H1 (DB с 2025-01-02).

## ЧАСТЬ 1 — H-082 Pyramiding по мульти-каналам (Mulvaney)
- Идея: несколько Donchian-каналов (90/126/252 бар) как уровни подтверждения. Вход на пробое первого; при пробое каждого следующего — **добавление юнита** к позиции (scale-in). Размер каждого добавления = % риска (старт 1%, макс 5%) на базе стоп-дистанции (ATR).
- **ВАЖНО (INSIGHT-002):** НЕ делать классический inverse-vol size (это режет high-vol, где наш edge). Сайзить по **conviction** (число пробитых каналов), не по ATR-стопу.
- Валидация: shadow vs single-entry на disjoint OOS; метрики: net/trade, **% трейдов с доходностью >100%** (right-tail capture), skew equity, DD, avg bars in trade. Purged walk-forward. H-058 гейтинг.
- Не менять ensemble-входы (donchian уже есть) — только модуль scale-in поверх сигнала.

## ЧАСТЬ 2 — H-083 Hierarchical Risk Parity (HRP) allocation
- Идея: cross-sectional аллокация через иерархическую кластеризацию корреляций (вместо нашего cap25%/equal-weight) — снижает концентрацию в коррелированных RUAL/NLMK.
- Метод: корреляции доходностей → квадратичная кластеризация → inverse-variance внутри кластера → агрегация. Risk-based, без прогноза доходности.
- Валидация: сравнить HRP vs equal-weight vs cap25% на disjoint OOS: net/PF/DD, концентрация (Херфиндаль), Capital Utilization. Purged walk-forward для ковариации.
- Примечание: пока на 5 базовых; расширение на отобранный пул H-080 — после его стабилизации.

## ЧАСТЬ 3 — H-084 Microstructure (VPIN / Amihud)
- VPIN: объём, классифицированный по знаку цены (buy/sell pressure), нормированный на окно → индикатор токсичного потока. Amihud = |return|/volume (illiquidity).
- Использовать VPIN как **entry-фильтр**: не открывать при токсичном потоке (верхний квантиль VPIN). Amihud как gate для H-079 (T-cost риск).
- Валидация: H-058 на VPIN-фильтре (отфильтрованные сделки должны улучшить net/trade при coverage≥0.5). Amihud корреляция со slippage.
- Roll/Kyle НЕ считать (нужны тики/L2 — нет данных); отметить как blocked.

## ЧАСТЬ 4 — H-085 Factor IC / quantile attribution
- Формализация атрибуции сигналов (строгий вид EXP-003, WorldQuant-стиль): для каждого сигнала IC = rank-corr(signal, future_return); quantile-портфели long-top/short-bottom; ICIR, t-stat.
- Показать, какие из 7+ функций (+Vortex/ADX) имеют значимый IC → кандидаты в ансамбль. Telemetry-only.
- Валидация: IC значим (|IC|>0.02, t>2) на disjoint OOS → signal retained.

## Сквозно
- last-closed bar, purged walk-forward, H-058/H-059 дисциплина. Никаких изменений baseline/config_hash 1c7f75dc44c2aa67; quorum=2; НЕ weighted votes; НЕ VWAP-removal.
- Деливераблы: `reports/h082_pyramiding.json/.md`, `reports/h083_hrp.json/.md`, `reports/h084_microstructure.json/.md`, `reports/h085_factor_ic.json/.md` + `orchestrator submit`.
