# MEMORY.md — единая память проекта Deeptrading

**Правило:** весь контекст, договорённости и статус — СЮДА. Файл читается первым.
Детали — по ссылкам. Не плодим новые md без нужды. Хроника сессий — в git-истории и
`docs/results/`.

---

## 🚀 НАЧАЛО СЕССИИ — ОБЯЗАТЕЛЬНО ПРОЧИТАЙ (актуально 2026-09-07)

**Сводка последней сессии (все результаты, изменения кода, карта, токены, статус live-бота):**
👉 **`docs/results/SESSION_SUMMARY_2026-09-07.md`** — начни с него.

**Порядок входа агента:**
1. `MEMORY.md` (этот файл) → 2. `docs/results/SESSION_SUMMARY_2026-09-07.md` → 3. `AGENTS.md`
4. Нужные контракты в `docs/`, реестр скриптов `docs/roadmap/SCRIPTS_INDEX.md`

---

## Как устроены файлы (карта)

| Файл | Что это | Когда писать |
|------|---------|--------------|
| `MEMORY.md` | **главная память, этот файл** | при любом изменении статуса |
| `docs/results/SESSION_SUMMARY_2026-09-07.md` | **сводка последней сессии** (карта, результаты, изменения, токены) | в конце каждой сессии |
| `docs/results/*.txt` | золотые отчёты тестов (FINAL_COMPARE, report_*) | по тесту |
| `AGENTS.md` | кратко агенту: инфраструктура, ограничения | редко |
| `docs/roadmap/MTF_V4_2026.md` | роадмап МТФ + журнал результатов | по ходу серий |
| `docs/roadmap/SCRIPTS_INDEX.md` | реестр ВСЕХ скриптов (~180, что делает каждый) | при добавлении скрипта |
| `docs/roadmap/DEV_PLAN.md` | план развития: единый движок + ускорение 10-30× | при правках движка |
| `docs/roadmap/ENGINE_ARCHITECTURE.md` | полная архитектура движка + как писать тесты | при изменении движка |
| `docs/roadmap/Sreaming.md` | план перевода на стрим-окружение | при работе со стримами |
| `docs/streams_architecture.md` | архитектура стримов T-Invest | при работе со стримами |
| `instruments.md` | White/Gray/Black списки по Net/PF/WR (живой) | при расширении |
| `chat.md` | переписка субагентов | только субагенты |

---

## 📊 ОТЧЁТЫ ПО ТЕСТАМ — В КАКОМ ВИДЕ ПОЛУЧАТЬ

**Все тесты должны давать «золотой отчёт»** (полный, где каждую муху видно), НЕ одну строку net.

**Требуемый формат (пример — `docs/results/report_semi.txt`, отчёты S5):**
1. **PER-TICKER DETAILED**: Trades/Win/Loss/WR%/GrossW/GrossL/Comm/Net/PF/MFE/MAE/Bars/индикаторы
2. **PER-TICKER × REGIME** матрица: N / WR% / Net по ячейкам
3. **PER-REGIME**: WINS vs LOSSES с индикаторами (RSI/ATR%/EMA/ADX/Vol на вход/выход)
4. **EXIT REASONS** по режимам: signal_exit/target/stop_loss с N/WR/Net и SL_dist/TP_dist
5. **FLIP vs NON-FLIP** по режимам и total
6. **SL/TP BREAKDOWN**, **EXIT QUORUM VOTES** (кто закрывает)
7. **VOLUME RATIO** wins vs losses (если доступен)
8. **SESSIONS** (утро/день/вечер) разбивка — см. `FINAL_COMPARE.txt`
9. Сводная таблица конфигов в конце

**Инструменты генерации (все в `backend/scripts/`):**
- `analyze_full.py` — полный отчёт из jsonl-дампа сделок (per-ticker, per-regime, сессии, флипы)
- `final_compare.py` — единый файл сравнения нескольких конфигов (для A/B тестов)
- `session_compare.py` — разбивка утро/день/вечер по конфигам
- `s5_diagnostics.py` — золотой отчёт S5 (самый полный, шаблон для всех)
- `per_ticker_dump.py` + `portfolio_merge.py` — быстрый портфельный бэктест

**Золотые отчёты-примеры (что должно получаться):**
- `docs/results/FINAL_COMPARE.txt` — 4 конфига flip сравнение с сессиями/режимами
- `docs/results/report_full.txt`, `report_semi.txt`, `report_none.txt` — золотые отчёты

---

## 💰 ЗОЛОТЫЕ НАРАБОТКИ (что реально даёт доход)

| Конфиг/скрипт | Результат | Статус |
|---------------|-----------|--------|
| **Semi-flip** (в NEUTRAL закрыть без входа) | **+55% к baseline** (+77K vs +49K, WR 65.6%) | ✅ подтверждён |
| **Optuna per-ticker** (20 тикеров) | **+13.8% OOS** (w2+w3), +89.7% train | ✅ параметры в БД |
| **5m-вход + 1m-исполнение** (entry_tf=5min) | кратно лучше 1m (RUAL PF 11.6 vs 3.2) | ✅ проверено на неделе |
| **entry_session=main** | лучше all на 10/10 (2 месяца) | ✅ подтверждено |
| **Портфель 10K, 20%/позицию, semi-flip** | +101% (июль неделя) | ✅ портфельная модель |
| SL×4 (ATR) | оптимален (×3/×2 хуже) | ✅ |
| **Live-бот sandbox** | 20 тикеров, semi-flip, optuna | 🟢 работает :8000 |

**Где золотые параметры:** `instruments.optuna_params` (БД, JSONB) — 20 тикеров с sl_mult/rr/quorum/vol/активные стратегии.

---

## 🧠 КЛЮЧЕВЫЕ ВЫВОДЫ (проверено, не переоткрывать)

1. **Semi-flip = менеджмент позиции, не фильтр.** В NEUTRAL: закрыть и ждать, не переворачиваться.
2. **Volume — единственный разделитель win/loss** (wins vol 2.05 vs losses 0.91), но как жёсткий порог НЕ работает (Optuna выбрала vol=0 для 16/20).
3. **RSI ~50 для win и loss** — бесполезен как фильтр.
4. **Вечерняя сессия — слив** (WR 40-50%) во всех конфигах. День — золото (net/t +48...+58).
5. **Pullback_ema закрывает 35% сделок** — стержень ансамбля.
6. **Все 7 стратегий ВСЕГДА в quorum.** `inc_<sid>` в Optuna = лишь кому тюнить параметры.
7. **Валидация свечей обязательна**: 22.08.2026 был сбой T-Invest (мерцание цен AFLT/MVID/NLMK). Битый день = 21% ложной прибыли.
8. **GMKN прошёл сплит ~24x 24.08.2026** (2900→122₽) — реальное событие, не баг.
9. **T задвоен**: TCS80A107UL4 (новый, данные) vs BBG000BSJK37 (старая, пусто).
10. **Маржа в бэктесте**: и LONG и SHORT замораживают обеспечение; полный реинвест × плечо = нереалистичная лавина.

---

## 🏗️ ДВИЖОК / СТРИМЫ — НОВЫЕ РАЗРАБОТКИ

**Архитектура:** `docs/roadmap/ENGINE_ARCHITECTURE.md` (полный аудит, как писать тесты, баги).
**План ускорения:** `docs/roadmap/DEV_PLAN.md` — профиль показал 71с/месяц/тикер, из них:
- `_signal_quality` 54% (13M lambda min/abs) — кэш/векторизация
- ATR в plan_entry O(N²) на каждый вход — кэш
- 20M `total_seconds()` на datetime — int-таймстампы
- **Потенциал 10-30× БЕЗ Rust.** Rust/стриминг — после ускорения.

**Стримы:** `docs/roadmap/Sreaming.md` (план), `docs/streams_architecture.md` (архитектура).
Уже реализовано: StreamManager (positions/trades/orders), CandleFeed gRPC+fallback,
reconcile loop. НЕ реализовано: StreamingEnsemble (инкрементальный compute_ensemble).

**Единый движок (цель):** runtime.py должен гонять тот же конвейер, что compute_ensemble
(backtest_v2.py уже делает это через реальный runtime + FakeDatetime).

---

## 🔧 ОПЕРАЦИОННЫЕ ЗАМЕТКИ (актуально)

- **Проект:** `/Volumes/Dev/Deeptrading` (= `/Users/Denis/Dev/Deeptrading` на .54), git на месте.
- **Инфраструктура:** 2 машины. Код правим на .7 (opencode), .54 = Postgres+backend+frontend.
- **Backend .54:** `cd ~/Dev/Deeptrading/backend && nohup .venv/bin/python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000 > /tmp/uvicorn.log 2>&1 &`
- **SSH .54:** `sshpass -p '0987' ssh Denis@192.168.1.54` (иногда таймаутит — повторить).
- **SSH .5 (Windows, та же БД):** `sshpass -p '0987' ssh nadts@192.168.1.5`, запуск через `wmic process call create "cmd /c ..."` или bat-файл.
- **Frontend .54:** vite :5173. **⚠️ управляется launchd `com.denys.vite-frontend` (KeepAlive). НЕ убивать kill!**
  - Перезапуск: `launchctl kickstart -k gui/$(id -u)/com.denys.vite-frontend`
  - Остановка: `launchctl bootout gui/$(id -u)/com.denys.vite-frontend`
- **БД:** `.54:5432 deeptrading/deeptrading`. Часовой пояс ВСЁ по Москве (UTC+3). ts в БД = UTC, метка бара = закрытие.
- **Секреты:** реальные токены только в `.env` (не в git) и `live_broker.py` (в .gitignore). Шаблон без токенов: `live_broker.py.example`.
- **Тесты движка:** `cd backend && .venv/bin/python3 -m pytest tests/ -q` (сейчас ~206 зелёных).
- **Не убивать чужие uvicorn** (могут быть параллельные процессы пользователя).

---

## 🤝 ДОГОВОРЁННОСТИ КОМАНДЫ

- **Единая рабочая папка `/Volumes/Dev/Deeptrading`.** Не создавать копий. Временное — в `~/Documents/Default Project`.
- Каждый правит свои файлы; перед правкой `git status`, чтобы не затереть чужое.
- Договорённости и статус — в этот файл (chat.md не единственный носитель).
- **ЕДИНЫЙ ДВИЖОК для тестов и живой торговли** — требование владельца (тест = бот).
