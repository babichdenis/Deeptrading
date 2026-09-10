# SESSION_SUMMARY_2026-09-10 — Аудит движка: регрессия bias_tf_sec, метрики-мониторинг, live-наблюдение

> **Дата:** 2026-09-10. Полный аудит CPU/памяти модулей движка + встроенная
> система метрик `_metrics_loop` + наблюдение за live-торговлей.
> **Главная находка:** коммит `4a295d0` (утро 10-го) тихо сломал генерацию сигналов:
> `_run_pipeline` использовал `bias_tf_sec`, не получая его → `NameError`, а
> `on_bar` глотал ошибку через `except Exception: return None`. Бот «работал», но
> **не торговал молча**. Починено. Второй баг — asyncpg `NOT IN (массив)` в
> `_hot_add_universe` (SBER/T вечно в pending). Тоже починено.

---

## 1. СИМПТОМ (что увидели по ходу аудита)

- Прямой вызов `compute_ensemble` упал: `NameError: name 'bias_tf_sec' is not defined` (строка 714).
- `git log -L` показал: коммит `4a295d0` заменил `// 3600` на `// bias_tf_sec`
  ВНУТРИ `_run_pipeline`, но параметр в сигнатуру/вызовы не добавил.
- Бот при этом `running=true`, свечи шли, но `signals_seen` стоял на 0
  → `ensemble_strategy.on_bar` молча глотал ошибку (`except Exception: return None`).
- Второй баг (найден ранее, 10-го): `_hot_add_universe` выполнял
  `figi NOT IN :skip` с массивом → `PostgresSyntaxError`, `except Exception: pass`
  глотал → `hot_adds=0`, SBER/Т вечно в pending.

## 2. КОРЕНЬ ПРИЧИНЫ

| Баг | Причина | Фикс |
|-----|---------|------|
| bias_tf_sec | параметр не прокинут в сигнатуру | `bias_tf_sec: int = 3600` в `_run_pipeline` + передача в оба вызова |
| NOT IN array | asyncpg не умеет `NOT IN ($1)` с массивом | `NOT (figi = ANY(:skip))` + логирование `last_error` |

Общий корень обоих багов: **паттерн «тихий сбой»** — `except Exception: pass` /
`return None` в hot-path. Любая ошибка = «бот работает, но не торгует» без следа.

## 3. АУДИТ CPU/ПАМЯТИ (что грузит, что нет)

- uvicorn-процесс (бот): idle ~0% между барами, всплески при 5м-закрытие.
- **`compute_ensemble` на 4320 барах 1m: 700–1400 мс** / прогон; гоняется на каждом
  5м-закрытии × 22 тикера. `list(buffer)` копируется на каждый бар (22×/мин, ~95k объектов).
- `broker.get_position()` на каждую свечу — НО кэш портфеля TTL=15с спасает (не gRPC каждый раз).
- **Нагрузка системы (load 31–39) — это Yandex-браузер + WindowServer + VM (Virtualization 30%), НЕ наш движок.**
- MOEX ISS часто отвечает ~30с (вместо 0.7с) → первый `GET /screener` 500-timeout, кэш потом спасает.
- persist (upsert батчем каждые 3с) в live оказался 1.7–5.8с в среднем — связано с нагрузкой машины.

### Потенциал оптимизации (записано в DEV_PLAN.md §4)
1. Кэшировать `compute_ensemble` между 5м-закрытиями / инкрементальный пересчёт.
2. Не копировать `list(buffer)` на каждый тикер.
3. `_oracle_coverage`/`_counterfactual_*` — в live не нужны (флаг).

## 4. СИСТЕМА МЕТРИК `_metrics_loop` (добавлена 2026-09-10)

- Каждые 30с сводит срез в `runtime.status["metrics"]` и пишет TECHINFO-строку.
- Метрики: `candles_per_sec`, `bar_ms_avg/max`, `ensemble_ms_avg/max`, `persist_ms_avg/max`,
  `persist_q/q5`, `universe_active`, `signals`, `seen/rejected/received`, `alerts[]`.
- **Реакция на аномалии** (`⚠ MЕТРИКИ:` + `events.log("METRICS_ALERT")`):
  - нет свечей >90с в торговую сессию;
  - очередь персиста >1900;
  - ensemble avg >2000ms;
  - bar avg >500ms.
- Сброс метрик в `start()`. Тайминги замеряются: весь бар (`_process_candle`), `on_bar`
  (ensemble), persist-батч.

### Наблюдения live (10:31–10:53 МСК, 22 тикера, платформа stream)
- cps≈0.55–1.15, bar avg≈430–545ms (max 35–37с — единичный затор машины после старта),
  ensemble avg≈280–470ms, persist avg≈1.7–3.3s. Очереди пустые, alerts зелёные.
- `DATA_STALE_CANDLE` 28 шт за `07:xx UTC` (возраст баров дорос до гейта 120с из-за
  нагрузки машины) — затем стабилизировалось, rejected не рос.
- Торговля: вход ALRS 20.52, MVID в трейлинге (стоп 44.03→44.52, +0.56%). Бот НЕ перезапускался
  после старта метрик.

## 5. ВАЖНЫЕ ПРАВИЛА (для будущих сессий)

1. **Бот в live — НЕ перезапускать без явного подтверждения пользователя** (после 10:30 МСК 2026-09-10).
2. **Никогда не глотать исключения молча** в hot-path бота: минимум `self._log`/`events.log`.
3. Проверка после правок: `GET /api/v1/bot/status` → `running=true`, `metrics` растут,
   `signals_seen` растёт на 5м-закрытиях.
4. `bias_tf_sec` теперь параметр `_run_pipeline` — при дальнейших правках не потерять.
5. Отчёт лучше сразу слать через `metrics["alerts"]`, а не парсить лог.

## 6. ФАЙЛЫ

- `backend/app/services/ensemble.py` — фикс bias_tf_sec (сигнатура + 2 вызова).
- `backend/app/bot/ensemble_strategy.py` — On_bar (было: тшио глотал ошибку).
- `backend/app/bot/runtime.py` — `_metrics_loop`, тайминги, `metrics` в status, сброс в start.
- `docs/roadmap/DEV_PLAN.md` — §4 «Живой мониторинг и hot-path runtime (аудит)».