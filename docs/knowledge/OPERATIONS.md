# Эксплуатация и окружение

## Карта инфраструктуры

Адреса из согласованной карты после 2026-10-01; доступность сегодня этой уборкой не проверялась.

| Машина | Адрес | Роль |
|---|---|---|
| MacBook «код» | 192.168.1.6 | Сессии разработки, доступ к серверному проекту через SMB |
| MacBook «сервер» | 192.168.1.7 | Основной checkout `/Users/Denis/Dev/Deeptrading`, backend/frontend, боевая Docker PostgreSQL 16 (`deeptrading-db`, `127.0.0.1:5432`) |
| Windows .8 | 192.168.1.8 | Копия проекта `X:\Deeptrading`, своя PostgreSQL 16.15 (`127.0.0.1:5432`), backend/frontend |

Проект на .7 лежит на локальном диске (`/Users/Denis/Dev/Deeptrading`) и доступен машине «код» как SMB-шара `/Volumes/Dev/Deeptrading`; работа через шару ломает git (SIGBUS), предпочитать локальный checkout сервера. Боевые данные — только в PostgreSQL 16 контейнера `deeptrading-db` на .7. `127.0.0.1:5432` на машине «код» — песочница PG14, не источник рабочих данных.

На Windows .8 развёрнута рабочая копия: проект `X:\Deeptrading`, отдельная нативная PostgreSQL 16.15 на `127.0.0.1:5432` (роль и БД `deeptrading`, данные восстановлены из дампа .7 2026-10-17), Python 3.11.9 в `X:\Deeptrading\backend\.venv`, `node_modules` установлены. Помощники запуска — `X:\run\run_backend.ps1` и `X:\run\run_frontend.ps1`. Старые адреса `.2`/`.3` не использовать. Наличие роли не означает, что бот или порт сейчас работает.

## Окружение

Backend: Python 3.11, FastAPI, SQLAlchemy 2 async, asyncpg. SDK `t-tech-investments`, импорт `t_tech.invest`. Frontend: Vite, TypeScript, lightweight-charts v5.

На SMB не устанавливать venv/node_modules. Локальный Python — `~/.venvs/deeptrading`; npm install/ci — только на машине с локальным диском проекта. Конфигурация — `backend/app/config.py` и непубликуемый `.env` соответствующего хоста; значения токенов и паролей в документацию не копировать. Сверять выбранный host/режим перед любым действием с БД/брокером.

## Команды для отдельного согласованного запуска

Из каталога backend на .7: `~/.venvs/deeptrading/bin/uvicorn app.main:app --reload` либо `zsh dev.sh` с подходящим локальным окружением. Frontend на сервере: `npm run dev` из frontend.

На Windows .8: бэкенд — `X:\Deeptrading\backend\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --loop asyncio` (быстрый путь — `X:\run\run_backend.ps1`), фронт — `npm run dev` из `X:\Deeptrading\frontend` (`X:\run\run_frontend.ps1`). Оба подняты задачами планировщика: `dtbackend` (обёртка `C:\Users\nadts\start_backend.ps1`) и `dtfrontend` (обёртка `X:\run\start_frontend.ps1`, пишет `X:\run\frontend.out`/`.err`). Перезапуск: `schtasks /Run /TN dtbackend` / `dtfrontend`. UI доступен с других машин по `http://192.168.1.8:5173/` (Vite `host: true`, `/api` проксируется на `127.0.0.1:8000`). Для прогонов из БД реплей по умолчанию идёт в режиме `CANDLE_PREFLIGHT=warn` (свечи только из БД, без дозагрузки); явный env по-прежнему перебивает. Эти команды — справка, не стартовый шаг новой сессии.

Сервис frontend на сервере по прежним операционным заметкам управляется launchd `com.denys.vite-frontend`; Windows — планировщиком. Не убивать произвольные процессы пользователя и не перезапускать всё ради проверки одного файла. Изменения на SMB видны серверу сразу; его autoreload может подхватить правку.

## Торговые и разрушающие операции

Действующее исследовательское назначение не разрешает bot/orders/deploy/API/backfill/DB-write. Старые указания «сначала запусти бота» отменены. Согласование отдельной операции не переносится автоматически на другие счета и хосты.

Сброс sandbox — через backend `POST /api/v1/sandbox/reset` и клиент `backend/scripts/reset_sandbox_account.py`; он меняет аккаунт/настройки/историю и может запускать бота. Это не проверка доступности и не безопасная стартовая команда. При отдельном разрешении руководствоваться актуальным кодом, не скопированным старым SSH-паролем.

## Диагностика

Runtime-постоянная запись и телеметрия выделены в отдельные модули; ошибки должны иметь ограниченные группы и переход восстановления, без утечки токенов. Настройки уровня логов — в config. [Последний аудит](../Audit/LARGE_CODE_REFACTOR_2026-10-15.md), [история логирования](../Audit/LOGGING_REAUDIT_2026-10-04_7d15183.md).

15 октября git через SMB возвращал SIGBUS: это ограничение конкретной сессии, не доказательство повреждения репозитория. Не делать reset/clean или переинициализацию; при недоступном git сохранить patch/hash-происхождение, Git-операции выполнять в локальном серверном checkout при отдельной необходимости. Если шара отключилась, не продолжать на старой локальной копии.


## Передача следующей сессии: песочница, 2026-11-15

Текущий этап — счёт песочницы и проверка API; новые источники/новости и массовую проверку свечей пока не начинать. Пользователь разрешил тестовые заявки в песочнице для проверки полного пути, но автоматическая стратегия выключена. Итоговая цель прибыльности не достигнута. Worker на .8 проверяет корневой chat.md примерно каждые 10 минут; последний ACK ARCH155 подтверждает работу над изоляцией счетов. Не считать отсутствие мгновенного ACK зависанием.

### Точное текущее состояние

- Новый sandbox-счёт `Deeptrading-baseline-20k-20261115`, исходный капитал **20 000 ₽**. Ранее названные 200 000 исправлены пользователем ДО пополнения. SDK и HTTP подтвердили 20 000 cash/equity, 0 PnL, 0 позиций/заявок. Старый счёт оставлен для истории; глобальных удалений не было.
- Настоящий account ID брать из `backend/.env` через settings, не из этого документа; сверять с `backend/data/sandbox_baseline_20k_20261115.json`. Там же предыдущий портфель, заявки и локальный baseline. Токены не копировать в отчёты.
- `BOT_MODE=sandbox`, `data/run_state.json` mode=sandbox. В Windows wrapper `C:/Users/nadts/start_backend.ps1` BOT_AUTOSTART=0. Бот не запущен. Backend/frontend доступны на .8:8000/.8:5173 при последней проверке.
- dtbackend/dtfrontend: 3 повторных запуска через 1 минуту, лимит 72h снят, wrappers передают exit code дочернего процесса. Это настройка восстановления, НЕ доказательство crash/boot recovery. Backup: `X:/run/backups/sandbox-service-20261115T225704`; предыдущая конфигурация и логи — `X:/run/backups/sandbox-view-20261115T204424Z`.

### Проверено живыми запросами

`backend/data/sandbox_capabilities_20261115.json`: 8/8 запросов успешны. Positions: 20 000 RUB, blocked0, securities/futures/options пусты. Operations: одно исполненное пополнение20 000. WithdrawLimits:20 000, блокировок нет. MaxLots для SBER: cash BUY66, margin BUY398, cash SELL0, margin SELL332. Это наблюдённые лимиты на момент проверки, не доказательство исполнимого плеча или модель расходов; публичное упрощённое описание sandbox2x не заменяет фактический ответ.

SBER `BBG004730N88`: lot1, шаг0.01 в BOTH sandbox и production metadata (production использован только для чтения справочника, без счёта/заявок). Старые исторические лоты не переписывать текущим справочником. GetTradingStatus: NOT_AVAILABLE_FOR_TRADING, market/limit false при api_trade_available true. Последний флаг не означает возможность отправить заявку прямо сейчас.

TradingSchedules(MOEX) из sandbox:15ноября нет торгов;16ноября returned regular06:10–15:54:59UTC (09:10–18:54:59МСК), openingauction06:00UTC (09:00МСК). Это ответ конкретного календаря, не универсальное расписание всех инструментов/вечерних сессий. Перед заявкой заново получить instrument trading status. Старое «в6» пользователь уточнил как московское время, не плечо и не машину;06/18 осталось неуточнённым. Не назначать автоматический запуск из этой фразы.

### Тест заявки — результат и ограничения

`backend/data/sandbox_order_cycle_20261115.json`:1лотSBER MARKET BUY через реальный `LiveBroker._api_post_order`, принудительно sandbox, ограничение проверки nominal<=5000. Брокер вернул INVALID_ARGUMENT30079 Instrument is not available for trading. После отказа отдельный SDK запрос подтвердил cash20 000,0невалютных позиций,0заявок. Продажа не отправлялась.

Доказаны маршрутизация/авторизация/отправка/отказ и отсутствие остаточной позиции. НЕ доказаны исполнение, отмена принятой заявки, закрытие, комиссии, идемпотентное восстановление после сетевой неопределённости или запись сделки полным runtime. Прямого HTTP open exactly1lot без запуска стратегии в проекте не найдено; HTTP read endpoints проверены отдельно. Не объявлять этот пробник полным end-to-end бота.

`python -m scripts.ops.sandbox_order_cycle` НЕ перезапускать вслепую: он запрещает перезапись существующей квитанции. Для следующего прохода сначала прочитать intent/order IDs, сверить брокерский счёт и исходы; затем подготовить новую уникальную квитанцию, проверить доступность торгов и только после этого тестировать1лот. Ошибка после отправки не означает, что заявка не принята. Не повторять funding и не вызывать старый reset endpoint.

### Незавершённое перед торговлей

1. ARCH155 у worker: nullable broker_account_id + index; идентичность из активного broker при записи; одинаковая фильтрация account+mode во всех чтениях, закрытиях, reconciliation/rebuild/orphan. Legacy NULL остаются историей неизвестной принадлежности, без blanket backfill/delete. Миграцию сначала посмотреть, потом применить. Два разных счёта с одинаковым FIGI не должны менять строки друг друга.
2. Сейчас HTTP stats и trades показывают18старых записей; worker также увидел reconcile delta_pnl−81. Это НЕ сделки нового счёта. `backend/reports/architect_20261115/sandbox_baseline_acceptance_r1.json` фиксирует ровно2failed checks (trade_stat_zero/trades_empty). После исправления и reload выполнить из backend: `.venv/Scripts/python.exe -m scripts.ops.verify_sandbox_baseline --output reports/architect_20261115/sandbox_baseline_acceptance_r2.json`. Выход0 требуется только ДО первой настоящей тестовой сделки; после начала сделок нулевой baseline уже не критерий.
3. Старый `/sandbox/reset` закрывает несколько одноимённых счетов, глобально удаляет историю и может запустить runtime. Не использовать до исправления. `prepare_sandbox_20k.py` — выполненная одноразовая операция с квитанцией, НЕ общий механизм reset и НЕ команда запуска каждой сессии.
4. SDK стакана имеет поле `orderbook_ts`, а существующий сервис читает `order_book_ts`: API ts=None при доступной метке источника. Исправление и единицы lot/depth у worker. Наличие цен не доказывает свежесть или возможность торговли.
5. Hourly core/service: старые4ошибки устранены,5architect acceptance проходят с явными market_id+target_figi. Полное runtime wiring/parity и автоторговля этим не приняты.

Следующая сессия начинает с MEMORY → CURRENT_STATE → этот раздел → новые сообщения chat.md. Сначала принять изоляцию счетов, затем завершить реальный sandbox lifecycle в разрешённую сессию. Исторические результаты RSI (+14.01%сентябрь,−5.55%август до funding) не являются результатами нового счёта.


### ARCH160: блокеры полного пути исполнения (проверено offline)

На .8 выполнен `python -m scripts.ops.probe_broker_contract_offline`. Артефакт: `backend/reports/architect_20261115/broker_contract_offline_r1.json`, hashes кода внутри. Пробник не обращается к брокеру/БД: настоящие методы LiveBroker с подставными SDK-ответами и session в памяти.

- Вход runtime передаёт `price_is_filled`, которого нет в сигнатуре LiveBroker: TypeError до запроса. Прямая успешная проверка SDK этого не обнаруживает.
- При requested2/executed1 метод входа возвращает только цену, а закрытие создаёт сделку на все2. Runtime статически считает положительную цену полным входом и возвращённую сделку полным выходом.
- Timeout пробрасывается наружу, общий runtime except считает его отказом. Неопределённый исход нельзя автоматически повторять: нужен сохраняемый intent/order ID и reconciliation, включая рестарт.

Исправление поручено worker в ARCH160 после изоляции счетов ARCH155. Критерии: единый контракт paper/live, фактически исполненное количество и остаток, различение отказа/неопределённости, восстановление без дубля заявки/денежных потоков. Production-код архитектор не менял, сценарии runtime целиком не исполнялись. Перед стартом проверить сдачу worker и scoped regressions. Документация полей: https://developer.tbank.ru/invest/services/orders/methods . У PostOrderResponse и OrderState разные описания executed_order_price; парсинг сверять для конкретного ответа.


### ARCH161: стакан и фильтр входа

HTTP стакана теперь содержит ts и честный state=empty. Исправлен gate_orderbook: пустой/односторонний/пересечённый/несогласованный стакан и отсутствующие/нечисловые активные метрики запрещают вход.36 целевых проверок проходят, отдельно11 проверок сервиса. Артефакт backend/reports/architect_20261115/orderbook_gate_acceptance_r1.json. Backend не перезапускался: проверить загрузку исправления при следующем согласованном рестарте. Свежесть, будущие timestamps, fallback last_price_ts и перевод количества лотов в рублёвую глубину остаются отдельными ограничениями; state=ok не доказывает актуальность. UI в этой проверке не осматривался.


### ARCH162: браузерная приёмка нового счёта

Реальный интерфейс5173 показывает20 000 ₽ equity/cash/free funds,0PnL,пустые позиции и выключенного бота. Старая подпись sandbox·10k и сентябрьская история всё ещё видны: это неполная изоляция UI, не операции нового счёта. На главном экране панель стакана не найдена; доступность API не считать готовностью просмотра стакана. Артефакт sandbox_browser_acceptance_r1.json. Кнопки управления/синхронизации не нажимались. Само открытие UI включает существующие опросы; в логах видны GetCandles/ensure_1min. Следующим сессиям учитывать это при read-only просмотре интерфейса.


### ARCH163: сохранность истории при миграции счетов

До миграции сохранены отпечатки53 строк sandbox/live: `backend/reports/architect_20261115/account_history_before_identity_r1.json`. Это все строки этих режимов, не только18 закрытых сделок UI. Поле broker_account_id на момент снимка отсутствует. Снимок read-only, без содержимого сделок и открытых account IDs; не заменяет резервную копию БД.

После проверки и применения миграции из backend выполнить:

```text
python -m scripts.ops.audit_account_history_preservation --baseline reports/architect_20261115/account_history_before_identity_r1.json --output reports/architect_20261115/account_history_after_identity_r1.json
```

Требуются `comparison.preserved=true` и `identity_column_present=true`. Проверка замечает удаление, изменение любого прежнего поля и переатрибуцию старых строк. Новые строки перечисляются отдельно.3 целевых теста прошли. Не заменяет проверку разделения двух счетов и HTTP baseline ARCH157; не доказывает отсутствие ошибок runtime. Архитектор миграцию не выполнял.


### ARCH164: текущий контур LIVE, бот выключен

Последняя явная команда владельца — переключиться на live. Проверен production SDK: настроенный счёт доступен, cash0,позиций0,заявок0. Владелец сообщил о самостоятельном переводе20 000 ₽, но поступление пока не подтверждено. Песочничные20 000 сохранены отдельно.

.env BOT_MODE и run_state переведены в live; BOT_AUTOSTART=0 сохранён. Не использовать /bot/mode для простого просмотра: обработчик безусловно runtime.start даже при остановленном боте. Выполнен рестарт backend; последовательность End/Run дала гонку, затем подтверждён Ready/нет8000 и отдельный Run восстановил HTTP. Ответ LIVE/runningFalse/startingFalse/cash0/equity0/positions0. Локальные initial_cash909.39/PnL−909.39 ошибочно смешивают прежнюю базу; это НЕ реальный убыток. Исправление account/funding baseline обязательно.

Квитанции: data/live_connection_20261115.json, data/live_selection_20261115.json (предыдущее run_state); reports/architect_20261115/live_selection_acceptance_r1.json. Одноразовые scripts/ops/probe_live_connection.py и select_live_stopped.py не перезапускать поверх квитанций. Реальных заявок/переводов не было. Предложенное ужесточение freshness до30сек НЕ реализовано: пользователь сменил задачу.

API переводов: https://developer.tbank.ru/invest/services/accounts/faq_transfers . Поддерживаются межброкерские переводы и пополнение с банка, нужны права «Торговля и переводы» на все счета; вывод недоступен. Реальные дополнительные счета открывают в приложении: https://developer.tbank.ru/invest/intro/faq . Права текущего токена на переводы не проверены; перевод не назначен конкретными счетами/суммой и не выполнялся.


### ARCH165: фактическое пополнение live

Прямая сверка SDK21:29:58UTC подтвердила1000 ₽ cash/equity,0позиций/заявок. За окно с21:25:53UTC одна исполненная операция INPUT1000 от21:29:04UTC. Владелец планировал20000, остаток пока не подтверждён. Отчёт backend/reports/architect_20261115/live_funding_observation_r1.json. Повторная read-only проверка: `python -m scripts.ops.verify_live_funding --since 2026-11-15T21:25:53+00:00 --output reports/architect_20261115/live_funding_observation_r2.json`; нужен новый путь, не перезапись. Это наблюдение за окном, не доказательство полноты истории счёта.

Нынешний live API берёт35старых mode-only сделок, подставляет их PnL в base и выводит initial_cash как equity−PnL. Затем сравнение с тем же PnL может дать reconcile.ok=true. Это круговая проверка, не независимая сверка брокера. Не подменять фиксом фиксированного депозита; нужны идентичность счёта, начало периода и подтверждённые внешние потоки. Worker уведомлён. Исправления runtime/account ещё не приняты; бот выключен.


### ARCH166: несколько реальных счетов и свежесть стакана

Владелец подтвердил второй брокерский счёт. GetAccounts21:32:18UTC показывает только один OPEN/FULL_ACCESS,1000RUB,0позиций/заявок. live_accounts_r1.json. Это не доказательство отсутствия второго счёта: проверить давность открытия и область токена. Read-only повтор: scripts.ops.inspect_live_accounts --output reports/architect_20261115/live_accounts_r2.json. Выбор счёта не менялся.

В исходнике gate_orderbook добавлены обязательные state=ok и tz-aware ts; возраст не более30сек,будущее запрещено,тестовое orderbook_as_of допускает детерминированную проверку.44теста pass,артефакт orderbook_freshness_acceptance_r1.json. После предыдущего рестарта изменение ещё не загружалось; не считать runtime-проверкой. Замена last_price_ts на время стакана в сервисе остаётся недопустимым fallback,worker исправляет отдельно.


### ARCH167: все статусы брокерских счетов

scripts/ops/inspect_live_account_statuses.py теперь использует рабочий authenticated gRPC stub и установленный users_pb2.GetAccountsRequest(status=ACCOUNT_STATUS_ALL).21:35:05UTC подтверждён ровно один счёт,дата открытия2020-11-26; fingerprint совпадает. Артефакт live_accounts_all_statuses_r1.json. Второй не отфильтрован default статусом, но причина недоступности не доказана. Владелец не знает область токена; предложено посмотреть её в настройках,секрет не присылать.

Предыдущие REST попытки завершились CERTIFICATE_VERIFY_FAILED; верификация не отключалась. В установленном SDK новый grpc.users/protobuf поддерживает ALL, а legacy Client.users.get_accounts не принимает параметров. Для диагностики обновление пакета не потребовалось. Документация области токена: https://developer.tbank.ru/invest/services/accounts/head-account .


### ARCH168: live21000 и назначение мультисчетов

Владелец вернул средства на первый счёт; directSDK21:36:29UTC подтверждает21000RUB cash/equity,0позиций/заявок. live_funding_observation_r2.json. История за то же окно всё ещё содержит толькоINPUT1000; причина неполноты не установлена. Не считать разницу20k прибылью и не создавать синтетический подтверждённый денежный поток.

По явной просьбе владельца worker получил MULTI-ACCOUNT-LIVE-01 в chat.md: аккаунты/права токена,выбор без автостарта,изоляция истории/кэша/базы капитала,идемпотентные внешние потоки и неполнота данных,UI,чтение возможностей API переводов. Реальные переводы/сделки/открытие счетов этим пакетом не назначены. Миграция предварительно ревью; прежний53-row snapshot сохранить. Sandbox20000 отдельный и не тронут.


### ARCH169: поступления теперь подтверждены обеими выдачами

На21:38:21UTC cursor и повтор прежнего GetOperations показывают INPUT1000+20000 (одинаковые ID),совпадающие с21000cash/equity; позиций/заявок0. live_operation_pages_r1.json и live_funding_observation_r3.json. Предыдущее отсутствие20k в операциях разрешилось со временем: не доказан дефект старого метода. Баланс и история обновляются несинхронно; до подтверждения потока статус неполный,без синтетической прибыли. Не дублировать события из разных выдач. Источник перевода по этим данным не установлен.

Постоянное указание владельца: всё по UI сразу передавать worker через корневой chat.md; не задерживать отдельным согласованием и не дублировать его UI работу. Архитектор проверяет API,исполнение,учёт и принимает результат.


ARCH170 actual-runtime acceptance: tests/test_runtime_live_adapter_acceptance.py invokes real runtime+LiveBroker with mocked SDK only. Current1failed2.29s,zero SDK sends and ORDER_REJECTED; permissive **kwargs fake in previous tests missed interface mismatch. Worker must fix then pass this boundary before claiming runtime path operational. No real order/DB mutation in test.


### ARCH171: переключение песочница/live — текущий приоритет

Владелец требует быстро переключаться между песочницей и деньгами. Worker получил полный вертикальный срез backend+UI в ARCH171: переключение атомарного mode/account без рестарта приложения,раздельные кэши/балансы/история,никакого runtime.start/order/funding от выбора,обработка незавершённых заявок и защиты прежних позиций,гонки/поздние ответы,восстановление выбранного контура с autostart0. Не считать готовым до фактической сдачи и приёмки. Текущий /bot/mode без исправления опасен безусловным запуском.

06:55МСК попадает в утренний аукцион06:50–06:59; непрерывные торги с07:00 для допущенных инструментов. Источник: https://www.moex.com/n76476 и https://www.moex.com/media/az-aktsii-pify-obligatsii.pdf . Generic SDK calendar из прежнего отчёта не содержит утренний сегмент,не применять его как полное расписание. Перед заявкой нужны конкретный инструмент и текущие флаги брокера. Таймер/торговля на06:55 не назначены.


ARCH172 уточнение владельца: переключатель ТРЁХ режимов Live/Песочница/Тесты. Выбор не запускает торговлю или replay; сохранённые параметры теста не стираются переходом в другой режим. Проверить6направлений,изоляцию результатов и восстановление без автостарта. Передано worker как поправка ARCH171.


ARCH173 acceptance: tests/test_mode_selection_acceptance.py invokes actual current mode handler with mocked runtime/persistence.6failed3.39s:4auto-start,2test_name-required. Это намеренно красная приёмка ожидаемого поведения,не поломка брокера и не полный suite. Worker исправляет выбор/запуск; адаптировать к новому selection API при его введении,но не ослаблять смысл проверки. Артефакт mode_selection_acceptance_r1.json.


ARCH174: владелец требует личной приёмки всех3режимов архитектором. Actual adapter read-only сравнение21:45:33UTC: sandbox20000/live21000,разные счета,0позиций/заявок (broker_context_comparison_r1.json).13проверок run_state_isolation/replay_runner pass2.42s; это fixtures,не полный replay по реальным данным/через UI.6mode-selection и1runtime-adapter проверки всё ещё падают. Не объявлять все режимы исправными. Live adapter сейчас всегда передаёт confirm_margin_trade=True; worker должен связать это с явной политикой плеча,вопрос владельцу ещё без ответа.


ARCH175 RESULT: исправлена сигнатура LiveBroker.open_position (optional price_is_filled). Неподтверждённая цена в настоящем runtime теперь pending без второй отправки;7целевых проверокpass,затем3adapter acceptancepass с двумя новыми случаями цены. Отправлена103.5 от брокера,план100неподставляется при любом значении флага. runtime_adapter_acceptance_r2.json. Backend не перезапускался; частичные исполнения/таймауты/изоляция/переключение не закрыты. Исправление передано worker,узкая зона освобождена.


### ARCH214 — явное включение ручного юниверса
Файл `X:\run\custom_universe.json` больше не должен включаться одним своим существованием. Для намеренного ручного списка явно задаётся непустой `DEEPTRADING_UNIVERSE_OVERRIDES` с путём. Отсутствующая/пустая переменная отключает загрузку и очищает кэш. Исторический файл сохранён; после смены выбора ранее созданные runtime-настройки требуют контролируемой перезагрузки. Исправление исходников не означает, что старый серверный процесс уже его загрузил.


### Migration revision collision (ARCH368)
Two historical files declared revision0002. Consolidated into `0002_broker_account_id.py`, preserving log source/request_id and sandbox account operations; duplicate file removed. Existing stamped revisions do not prove both parts ran. Before upgrading a different database, inspect both schemas;0006 rejects missing/incompatible legacy columns. Readonly .8 audit found0005 with both columns/indexes. Migration source repair and offline graph/DDL checks do not mean deployment or PostgreSQL execution acceptance. Audit: `backend/reports/connected_week_20261109_13_c1/migration_state_r1.json`.


### Migration-managed execution tables (ARCH373)
`create_bootstrap_schema` is used by applicationstartup and SignalLab. It excludes tables with `info.migration_managed=True`, currently broker_order_intents and broker_order_fills. Importing thejournal no longer lets startup create_all bypass0006. Deploy through explicit migrations and schema checks; absenttables must block submissions, not trigger ad-hocDDL. Four focusedoffline tests passed; no applicationstartup or productionmigration performed.
