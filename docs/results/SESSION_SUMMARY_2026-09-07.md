# SESSION_SUMMARY_2026-09-07 — Итоги сессии: Optuna, портфель, live-бот

> **Дата:** 2026-09-07. Что сделано за сессию, результаты тестов, изменения в движке,
> где что искать. Для будущих сессий — читать перед началом работы.

---

## 1. ГДЕ ИСКАТЬ (карта артефактов)

| Что | Где |
|-----|-----|
| **Код проекта** | `/Volumes/Dev/Deeptrading` (SMB-шара = репозиторий .54, git на месте) |
| **Реестр всех скриптов** | `docs/roadmap/SCRIPTS_INDEX.md` |
| **План развития движка** | `docs/roadmap/DEV_PLAN.md` |
| **Архитектура + аудит** | `docs/roadmap/ENGINE_ARCHITECTURE.md` |
| **План стримов** | `docs/roadmap/Sreaming.md` + `docs/streams_architecture.md` |
| **Роадмап МТФ (результаты S5)** | `docs/roadmap/MTF_V4_2026.md` §19-21 |
| **Результаты тестов (json)** | `backend/scripts/optuna_params_results.json`, `optuna_week_results.json` |
| **Полные золотые отчёты** | `docs/results/FINAL_COMPARE.txt`, `report_{full,semi,none}.txt` |
| **Шаблон live_broker (токены заглушены)** | `backend/app/bot/live_broker.py.example` |
| **Реальный live_broker (с токенами)** | `backend/app/bot/live_broker.py` — **НЕ коммитить** (в .gitignore) |

**Токены/аккаунты (sandbox):**
- Токен: `t.Qhvl9v-...` (в `live_broker.py:TOKEN`, закомментирован в `.env` строка 7 как `#sandbox=`)
- Account: `413306e6-f634-4aef-a553-c84e764b298a`
- Target: `sandbox-invest-public-api.tbank.ru`
- Боевой live-токен в `.env` `TINKOFF_TOKEN` (t.LwZSK...) — **НЕ ТРОГАТЬ**, не смешивать с sandbox

---

## 2. РЕЗУЛЬТАТЫ ТЕСТОВ (что показали)

### 2.1. Series 5 — flip-режимы (5 тикеров, изолированно 10K/акцию, август)
Файл: `s5_neutral_gate.py` / `s5_semi_sl.py`. Полный золотой отчёт в AGENTS §19.

| Тест | Net | WR% | PF | Вывод |
|------|-----|-----|-----|-------|
| A Baseline (SL×4) | +49,926 | 59.3% | 4.06 | — |
| B NEUTRAL-gate | +22,439 | 55.4% | 2.69 | **провал** (-55%) |
| **C Semi-flip (SL×4)** | **+77,348** | 65.6% | 3.97 | **+55% к baseline** |
| D Semi-flip SL×3 | +56,218 | 63.1% | 3.80 | тесный SL хуже |
| E Semi-flip SL×2 | +29,576 | 59.5% | 2.98 | ещё хуже |

**Вывод:** semi-flip (в NEUTRAL закрыть без входа в новый flip) — лучший менеджмент позиции. SL×4 оптимален.

### 2.2. Volume-фильтр (6 тестов, s5_matrix_full.py)
Тест 1 (Semi-flip + vol>=1.0) = +37,797 лучший, но **НЕ дотягивает до чистого semi-flip C (+77K)** — порог 1.0 слишком агрессивен. Per-regime quorum (Gate2/My) УХУДШАЮТ.

### 2.3. Optuna per-ticker (optuna_sweep_params.py)
Схема: train w1 (10-17.08), valid w2 (17-24.08)+w3 (24-31.08). 60 trials TPE/тикер.
Параметры: sl_mult(3-6), rr(2-6), quorum(2-3), vol_thr(0-1.5), inc_<7 стр>, + параметры стратегий.

**20 тикеров:**
| Неделя | Baseline | Optuna | Δ |
|--------|----------|--------|-----|
| w1 (train) | +60,748 | +115,254 | +89.7% |
| **w2+w3 (OOS)** | **+181,006** | **+205,920** | **+13.8%** |

- Стабильно лучшие: MAGN, NLMK, LENT, NVTK, CHMF, MTSS, SBER, SFIN, TRNFP
- Переобучились: GMKN (-9,221 w2), GAZP, ASTR, RUAL
- **Параметры сохранены в `instruments.optuna_params` (БД) для 20 тикеров**

**⚠️ КРИТИЧНО про `inc_<sid>`:** ВСЕ 7 стратегий ВСЕГДА участвуют в quorum. `inc_<sid>` = лишь выбор, КАКИМ стратегиям тюнить параметры (активные=optuna, неактивные=V2). НЕ "только эти торгуют".

### 2.4. Портфельный тест (общий пул 10K, 20% на позицию, июль 20-27)
Метод: `per_ticker_dump.py` (каждый тикер 1 прогон compute_ensemble) → `portfolio_merge.py` (общий пул, кто успел — тот взял).

| Конфиг | Exec | Net | Return | Max concurrent |
|--------|------|-----|--------|----------------|
| **Semi-flip** | 976 (21%) | +10,112 | **+101%** | 9 |
| Full-flip | 1,729 (40%) | +8,616 | +86% | 11 |
| No-flip | 1,085 (74%) | +7,275 | +73% | 9 |
| Full+margin | 3,953 (93%) | +58,521 | +585% | 19 |

**Semi-flip лучший и изолированно, и в портфеле** (после фикса бага SHORT).

### 2.5. Сессии (утро/день/вечер) — semi-flip портфель
| Сессия | N | WR% | Net/t |
|--------|----|-----|-------|
| morning | 159 | 68.6% | +11.52 |
| **day** | 584 | 69.3% | **+12.60** |
| evening | 233 | 49.8% | +3.96 |

**Вечер — слив во всех конфигах** (особенно NEUTRAL/HV: WR 36-44%). Рекомендация: тестировать без вечерней сессии.

---

## 3. ЧТО ИЗМЕНИЛИ В КОДЕ (для будущих сессий)

### 3.1. Валидация битых свечей
**Проблема:** 22.08.2026 сбой T-Invest — у AFLT (32→85), MVID (46→4222), NLMK (70→317) цена "мерцает" весь день. Одна битая сделка AFLT дала +7827₽ ложной прибыли (21% net!).

**Фикс в `app/services/ensemble.py::_validate_candles`:**
- Уровень 1: день отбрасывается целиком, если ≥3 прыжков >50% между соседними барами (мерцание)
- Уровень 2: бар отбрасывается если прыжок >40% от prev close

**Зеркало в live:** `app/bot/runtime.py::_candle_ok` — инкрементальная валидация каждой свечи стрима (prev_close обновляется только валидными). 206 тестов проходят.

### 3.2. Optuna-параметры в live-боте
**`app/bot/runtime.py::_build_ensemble_params(db, figi, ticker, lot, capital, sessions)`** читает `instruments.optuna_params` и строит EnsembleParams:
- setups = активные стратегии + их params (неактивные = V2)
- sl_mult/rr/quorum/vol_thr из optuna
- neutral_mode="semi_flip"

**Фикс SL/TP:** в `_execute_pending` SL/TP берётся из `strat.p.sl_mult/rr` (optuna), НЕ из `cfg.atr_multiplier` (иначе UI перезаписывает optuna).

### 3.3. Портфельная модель 20% в live
- `POS_PCT = 0.20` — позиция до 20% от equity (модель portfolio_merge)
- `ensemble_capital = real_cash × 0.20` (в логе "позиция до 2280")
- universe = все 20 eligible (top_n=20), select_eligible_universe ресемплит 1m→5m на лету (не требует 5m в БД)
- feed ходит на sandbox API (target=SB) с sandbox-токеном для mode=sandbox

### 3.4. Прочее
- `database.py`: pool_size=10, max_overflow=20 (был дефолт 5/10 — таймауты при 20 тикерах)
- `runtime._flush_persist`: добавлена запись 5m (ресемпл 1m→5m при закрытии бара)
- `portfolio_merge.py`: **фикс SHORT** — и LONG и SHORT замораживают обеспечение margin_req=notional/lev (раньше SHORT давал cash → 20 позиций сразу)

---

## 4. СТАТУС LIVE-БОТА (2026-09-07, sandbox)

- Backend: uvicorn на :8000, бот mode=sandbox
- top_n=20, позиция до 2280₽ (20% от 11400), semi-flip, optuna-параметры
- universe: все 20 eligible тикеров
- Frontend: vite на :5173 (через launchd `com.denys.vite-frontend`, KeepAlive=true)

**⚠️ Vite управляется launchd — НЕ убивать kill!** Перезапуск: `launchctl kickstart -k gui/$(id -u)/com.denys.vite-frontend`. Полная остановка: `launchctl bootout gui/$(id -u)/com.denys.vite-frontend`.

---

## 5. СЛЕДУЮЩИЕ ШАГИ (по DEV_PLAN.md)

1. **Ускорить compute_ensemble** (DEV_PLAN Шаг 1): `_signal_quality` = 54% времени (13M lambda min/abs), ATR в plan_entry O(N²) на каждый вход, 20M total_seconds() на datetime. Потенциал 10-30× без Rust.
2. **Единый analyze() слой** отчётов (все тесты через один формат золотого отчёта)
3. **Аллокация капитала** (Optuna по весам на portfolio_merge) — после фикса компаундинга
4. **Ограничить реинвест** в portfolio_merge (сейчас экспоненциальный компаундинг завышает)
5. **Отключить вечернюю сессию** в тестах (вечер = слив)
