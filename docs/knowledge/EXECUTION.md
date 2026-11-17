# Исполнение, расходы и капитал

## Базовый контракт движка

1. Стратегия видит только уже закрытые бары.
2. Решение signal policy фиксируется в audit, исполнение — next open.
3. Slippage направлен против сделки. Комиссии учитываются отдельно от gross.
4. Intrabar stop/target проверяются начиная с исполняемого бара; конфликт — STOP_LOSS_FIRST.
5. Гэп через stop исполняется по open, даже если он хуже stop.
6. На конце данных используется определённое контрактом принудительное завершение; отсутствие достоверной цены не даёт права выдумать её.
7. Один FIGI — одна позиция. Усреднение и пирамидинг запрещены; same-side решение обрабатывается policy.

В разных исследовательских версиях действуют дополнительные правила. Не переносить параметры одной версии в другую без нового config/hash.

## PARALLEL-DAY-1

Полный [контракт](../PARALLEL_DAY_1.md) важнее этого краткого пересказа:

- LONG_ONLY, будни, `10:00 <= decision MSK < 18:44`; закрытый 5m-бар даёт решение в момент доступности, соответствующий next 1m open имеет этот timestamp. Не добавлять лишнюю минуту.
- Выход по заданному горизонту или точному cutoff 18:44 MSK; ближайшая имеющаяся минута не подменяет отсутствующий endpoint.
- 5 слотов по 20% **начального** капитала 1.0, без плеча и реинвестирования. Допуск проверяет полный debit с расходами, поэтому пятый слот может не поместиться.
- Расходы на каждую сторону: 5/2 bps комиссия/slippage и чувствительность 10/5. Дробные количества — исследовательское упрощение, не исполнение лотов.
- Нет entry-минуты — SKIP; нет достоверного требуемого выхода — INCOMPLETE. Полный итог не считать как сумму только известных исходов.

## Неизвестный капитал

После первой принятой сделки с неизвестным выходом реализуемый последующий капитал неизвестен. Отдельно публикуются:

- genuine known-prefix equity **до** первой accepted-unknown;
- `final=None`, если полный путь капитала не установлен;
- closed-net-cashflows за весь период как диагностическая сумма известных закрытий.

Closed cashflows не заменяют капитал и не доказывают превосходство над cash. Сравнение политик требует общих известных дат и одинакового учёта неизвестных. Cash для NO_SELECTION — самостоятельный валидный исход; незаданный порог не заменяется 0.5.

## Паритет с runtime

Бот прогревается историей около трёх дней 1m из БД; ensemble требует не менее 120 баров. Replay/parity должны воспроизводить тот же прогрев. Малое число сделок при холодном replay ещё не является дефектом торговой логики. Обещание единого движка не заменяет сверку реальных путей исполнения.


### 2026-11-16 — подтверждение владельца: факт исполнения задаёт брокер
Для live/sandbox фактические позиции, объёмы исполнения, цены, комиссии и операции счёта подтверждаются ответами брокера с идентичностью счёта/заявки/исполнения. Локальная запись — история и состояние бота, а не замена брокерского факта. Ошибка/таймаут не равны отсутствующей позиции или отказу; цена свечи и оценочная комиссия не становятся фактом исполнения. Неоднозначность сохраняется до сверки. ARCH193 исправляет только ошибку повторного чтения позиции; реконструкция закрытий и дубликатов требует дальнейшего исправления.


### ARCH362 — общий путь закрытия, ограниченная приёмка
Runtime-выходы (стоп, intrabar, сигнал, ручное закрытие, сокращение портфеля, удаление тикера, EOD и конец replay) направлены через `_submit_close_reconciled`. Подтверждение определяется по идентичности заявки, фактическому количеству и конечной положительной цене; неизвестный/частичный результат сохраняет pending и защиту.114focused тестов проходят. Это не durable-журнал: рестарт, поздние исполнения и сквозная идентификация алиасов ещё требуют реализации/приёмки. Согласованность правил исследовательского раннера и live этим не установлена. Подробное основание: `backend/reports/connected_week_20261109_13_c1/close_lifecycle_acceptance_r1.json`.


### Durable close settlement contract (ARCH371)
`BrokerOrderJournal.settle` requires stored cumulative broker evidence, locks the scoped intent and atomically inserts only the unaccounted delta into PaperTrade plus BrokerOrderFill. `CloseSettlement.applied_units=0` means no new incremental bookkeeping; `trade_id` references the stored trade, `released` indicates terminal accounted request. `remaining_units` is request arithmetic, not a fresh portfolio reading. First delta requires a transient delta-sized trade with actual delta price/costs; cumulative average is not a delta price. Confirmed zero CANCELLED/REJECTED releases through observe; nonzero terminal requires settle. Duplicate/reordered/conflicting scope evidence cannot authorize a new submission.31focused tests include actual isolatedPostgreSQL concurrency and rollback aftertradeflush. Runtime wiring, persisted originalposition context and crash reconciliation remain incomplete.0006 is unapplied; startup create_all must not bypass its migrationguard.


### Original close-position evidence (ARCH375)
CloseIntent.position_snapshot stores ClosePositionSnapshot in the same reservation transaction. Decimal entryprice retains precision; timestamps serializeUTC; originalpositionunits/side mustmatch request. Actual entrytime requires execution_ledger or broker_history provenance. Portfolio snapshot time is not entrytime: current LiveBroker positions synthesize now, so do not relabel it. Unknown costs/time remainNone. CloseIntent.from_record validates restoration. Settlement checks storedcontext, compares price at PaperTrade6decimal scale and refuses unknownentrytime accounting instead of inventing it. Snapshot is optional only for legacy compatibility, not proof of recovery for omittedcontext. Independent entryhistory reconciliation and immediate/runtime integration remain outstanding.


### Broker order-stage evidence (ARCH376)
`broker_fill_evidence` parses GetOrderState stages with originalrequest/FIGI/side/lot validation and scopedlookup guards. OrderStage.quantity is lots, price perinstrument, execution_time actualexecution; stages mustcoverexactexecutedquantity andhaveuniquetradeIDs. Weightedprice uses stages, not ambiguous aggregatefields. PostOrderResponse and OrderState executed_order_price descriptions differ: do not silently reuse a parser or divide theimmediateresponse byquantity. Sources: https://developer.tbank.ru/invest/services/orders/methods and https://tinkoff.github.io/investAPI/orders/.19offlineSDKfixturetests passed; parser is not currentpositionlineage proof. Bind to originalentry request and verifyaccount/inventorycontinuity before snapshotpromotion. No historicalposition inferred from currenttime or latesttickerrow.


### Persisted entry-time provenance (ARCH378)
`_persist_entry_fill` queries originalorder state and validates stageunits/price before `_st_open` persists evidence. SandboxTrade.meta contains entry_time_source, entry_time_confirmed, entry_fill_evidence and separate entry_observed_at. Confirmedbrokerhistory updates columnentry_time; absentproof keepslegacy nonnull observationtimestamp but sourceunknown/confirmedNone. Recovery must never trust columnalone. Repeatedsameexecution preservesexistingproof through lookupfailure; changedquantity/price withoutproof invalidatesit. Mode/account captured acrosswrites; accountchangeduringcommit returns pending, preventsnewscope success.83focused actualentrymethod tests pass; realAPI/currentpositioncontinuity andallUI/analyticsconsumers notcertified.


### Broker status is not inferred from quantity (ARCH380)
Closeadapter uses SDK enum names. NEW remainsunresolved; REJECTED/CANCELLED areterminalonlyaccordingto actualreportedstate. PartialCANCELLED retains terminalstatus and releasesonlyafteraccountedquantity. Invalidstatus or contradictorystatus/quantity isunknown, never inferredFILLED.15focused checks include actualmethod NEWzero→newrequestID with onlyonesend. This is narrowacceptance; missingcontext/scope/identity guards inworkerintegration stillrequirecorrection.


### Close submission scope freeze (ARCH382)
LiveBroker.close_position captures mode/account before firstawait and sends with capturedaccount + expected_scope. _api_post_order validates binding before/afterservice lookup and usescapturedroutingmode; it consumes expected_scope ratherthan sendingit toSDK. Scopechanges retainunresolvedoriginalintent, suppresscrossscopecompletion.30focusedstatus/scope/cache checks passed on offlineadapter/temporarySQLite; generalaccountswitch lifecycle and PostgreSQLadapter recovery remainunverified.


### Startup close recovery (ARCH385)
Before live/sandboxstreams orruntime tasks, start restores unresolvedjournal intents into pendingcloseorders for the exactbroker scope. Originalrequest/quantity/lot/observed/accounted state retained; none authorize resend. Newentries paused whileuncertaintyrestored; missingstorage/conflicts/scopechanges abort. No fakeheldposition ormarkpricecreated. Emptyjournal preservesmanualpause.7focused realPostgreSQL/runtime method checks pass; completeautomaticreconciliation,latefillsettlement andresume remain outstanding. Existinglegacy in-memoryterminalcleanup must beconnectedtojournal before readiness.


### Durable close observation before cleanup (ARCH387)
All closepending reconciliation persists validated brokerstatus/cumulativequantity before terminalzero removal frommemory. Storagefailure retains pending/protection andpausesentries. Exactduplicate of alreadycommittedemptyterminal evidence acknowledges successfully, allowing recovery from DBcommit-before-memorycleanup; differentidentity/status doesnot. Nonzero observations remainpending until atomicsettlement andruntimeaccounting arewired.90focused checks passed including realPG failure/retry; complete latefill lifecycle notyetaccepted.


### Согласованность истории исполнения (ARCH388)
История GetOrderState не получает подтверждённое происхождение при NEW/REJECTED с ненулевым исполнением, FILL с неполным объёмом или PARTIALLYFILL с полным объёмом. CANCELLED может содержать исполненные сделки: они сохраняют значение для учёта. Противоречие не заменяется нулевым исполнением.


### Сверка позиций и незавершённые заявки (ARCH389)
Фоновая сверка не должна пересоздавать, закрывать как orphan или очищать дубликаты тикера, пока его вход/выход ожидает обработки или закрытие выполняется. Источник результата — исходная заявка и журнал исполнения. Остальные тикеры продолжают обрабатываться. Проверки перед изменением и commit защищают от обнаруженных внутри процесса пересечений; межпроцессная сериализация и гонка во время commit этим не доказаны.


### Учёт закрытия по исполнениям брокера (ARCH391)
`BrokerOrderJournal.settle(..., evidence=OrderFillEvidence)` строит новую сделку внутри транзакции под блокировкой исходной заявки. `BrokerOrderFill.execution_evidence` хранит идентификаторы, объёмы, цены и время только вновь учтённых исполнений. Повторные ответы должны содержать прежние исполнения без изменений; иначе учёт остаётся незавершённым. Цена новой части вычисляется по новым исполнениям, не по общей средней заявки.
Исходная позиция, подтверждённое время входа, внутренний счёт и основание комиссии берутся из сохранённого snapshot. Комиссия `configured_estimate` остаётся оценкой; фактические комиссии брокера этим не установлены. Нулевая ставка сохраняется. Уже учтённая часть без идентификаторов исполнений не восстанавливается молча по округлённой цене.
Путь добавлен в журнал, но ещё не вызван из фонового runtime: до подключения необходимо обеспечить durable acknowledgement локального учёта, чтобы settlement не освобождал слот и не исчезал из восстановления раньше завершения локальных действий. Это ограничение относится и к аудиту непосредственного исполнения. Миграция 0006 пока не применялась к рабочей БД.


### Незавершённый локальный учёт после settlement (ARCH392)
Фоновый runtime вызывает stage settlement с `defer_runtime=True`. `runtime_accounted_units=0` создаётся при первом учёте и не отключается последующими вызовами без флага. Уже учтённый объём без checkpoint нельзя автоматически объявить не обработанным. Пока локальный учёт не завершён, `unresolved_figi` сохраняется даже после полного исполнения и восстанавливается при старте.
`apply_runtime` блокирует исходную заявку, выбирает только новые связанные PaperTrade и вызывает обработчик с той же сессией БД. Обработчик не должен сам commit или выполнять внешние эффекты. Его изменения и счётчик подтверждения фиксируются одной транзакцией. Повтор пропускает выполненную часть; исключение откатывает обе записи.
Runtime запись позднего исполнения подключена, но фактический обработчик SandboxTrade и очистка состояния памяти после подтверждения ещё не подключены. Непосредственный путь адаптера сохраняет прежнюю семантику и требует отдельного подключения. Все новые проверки — в изолированной PostgreSQL, миграция рабочей БД не выполнялась. ARCH391 «runtime не вызывает stage settlement» этим обновлением заменён.


### Проекция закрытых частей в локальный портфель (ARCH394)
`ClosePositionSnapshot.local_position_id` и `entry_client_order_id` передаются парой и сохраняются в snapshot. `project_close_fills` работает только с этой исходной строкой, блокирует её и проверяет счёт, режим, инструмент, сторону, остаток, цену и подтверждённое время/заявку входа. Последняя подходящая строка не подставляется.
Через `apply_runtime` новая закрытая часть и уменьшение остатка фиксируются вместе с подтверждением журнала. Стоп/цель открытого остатка сохраняются; полное закрытие закрывает исходную строку. `exit_meta` связывает часть с PaperTrade, исходной позицией и обеими заявками, явно сохраняя основание оценки комиссии. Повтор обрабатывается счётчиком журнала.
Обработчик проверен на настоящих таблицах отдельной PostgreSQL, но фоновый runtime пока его не вызывает. Перед подключением нужны передача исходных идентификаторов, проверка остатка у брокера и идемпотентная очистка памяти после уже выполненного подтверждения. Для следующего закрытия остатка после частичной отмены нужно учитывать прошлые закрытые части в проверке происхождения входа; исходные исполнения входа не переписываются под остаток.


### Завершение позднего закрытия в runtime (ARCH395)
`_finish_close_runtime` подключён к pending-close reconciliation. Перед локальной проекцией сбрасывает кэш портфеля и сверяет остаток у брокера: для полного выхода нужна отсутствующая позиция; для частичного — точные FIGI, количество, сторона и цена входа. После атомарной проекции обновляет остаток защиты либо очищает полный выход. Терминальная заявка удаляется из pending; активная частичная сохраняется.
Если база уже подтверждена, а очистка памяти прервалась, повтор читает SETTLED с runtime checkpoint и завершает память без повторного PaperTrade/SandboxTrade. Legacy `_st_close` этот путь не вызывает. Ошибки и смена счёта сохраняют ожидание. Автоматического снятия общей паузы входов пока нет.
Это заменяет прежнее ограничение ARCH394 «фоновый runtime не вызывает проекцию». Непосредственный ответ адаптера всё ещё идёт старым путём; его объединение и полный паритет не завершены. Проверки использовали настоящую отдельную PostgreSQL и подменённые ответы брокера; рабочая БД и реальные API не запускались.


### Единое завершение немедленных и поздних закрытий (ARCH398)
`LiveBroker.close_position` возвращает `CloseSubmission` после durable observation, а не PaperTrade. Подтверждение отправки не является завершённым локальным учётом. Runtime сразу запускает тот же stage-based reconciliation для этого FIGI, который обслуживает таймауты. Цена и время сделки берутся из исполнений, комиссия остаётся явно расчётной. Нулевой тариф не заменяется запасной ставкой.
Статус `reconciled_closed` означает уже выполненную локальную проекцию и проверенный остаток брокера; вызывающие обработчики не повторяют `_st_close`. Непосредственный legacy-путь live/sandbox этим заменён. PaperBroker сохраняет свой детерминированный путь; полный паритет стратегии требует отдельной проверки.
Ручной `/positions/close` использует runtime, не вызывает адаптер напрямую и не переключается на sandbox при отсутствии позиции. `closed=False`/pending нельзя показывать как закрытую сделку. В кратком результате цена/PnL могут быть None, есть order_id для связи с учётом. Scope фиксируется до чтения портфеля и проверяется перед отправкой.
Общая пауза входов пока не разделяет ручную команду, риск и recovery; её безопасное автоматическое снятие остаётся следующим блоком. Продакшен и реальные брокерские API этими проверками не запускались.


### Владение паузой входов и проверка риска (ARCH399)
Ручная пауза и автоматические блокировки независимы. Ключ сверки закрытия включает режим, счёт и исходную заявку. Успешное завершение снимает только этот ключ после проверки свежего дневного PnL; другая заявка, ручная пауза или риск остаются. Ошибка риска удерживает pending до повторной проверки, даже когда локальная проекция уже записана.
`refresh_daily_pnl` для live/sandbox выбирает только текущий явный счёт/режим и интервал дня MSK до as_of, проверяет неизменность scope после чтения. Unknown/nonfinite экономический результат не считается нулём; duplicate_cleanup исключается как неэкономическая служебная запись. Запрос ручного продолжения проверяет риск, сохраняет только ручное намерение и может вернуть entries_paused=True. Причины доступны в entry_pause_reasons.
Это заменяет ограничение ARCH398 о неснимаемой recovery-паузе для проверенного пути закрытия. Кэш daily_pnl_cached пока сохраняет прежний fallback0 после TTL; его корректность, scope/date invalidation и весь риск-контур ещё не приняты. Полный startup и реальные сервисы не запускались.


### ARCH400: свежесть дневного риска
Дневной результат неизвестен при истёкшем кэше, смене счёта/режима/объекта брокера/торгового дня MSK или движении часов назад. UNKNOWN запрещает новые входы; числовой ноль допустим только после успешного чтения. Перед исполнением ожидающей покупки риск проверяется заново. Paper-источник использует sessions брокера и его default account. Это реализованный дневной результат, а не полная оценка риска портфеля.


### ARCH401: неопределённый исход открытия
Ошибки отправки и разбора ответа LiveBroker.open_position представлены EntrySubmissionUnknown. Runtime сохраняет исходные broker_scope/requested_lots и PENDING_RECONCILIATION; проверяет также замену объекта брокера после await. Исключение не доказывает отказ брокера. Повторная отправка в текущем процессе запрещена до сверки. Сохранение намерения до отправки и восстановление open после перезапуска ещё отсутствуют.


### ARCH402: намерения открытия и закрытия
Журнал различает action=open/close; сторона BUY/SELL не определяет назначение заявки. OpenIntent сохраняет исходные целые лоты, время, тикер, стратегию и уровни защиты. Незавершённое открытие и закрытие конкурируют за один FIGI в рамках mode/account. Общий журнал наблюдает исполнение, но close settlement/apply_runtime не принимает open. Этот контракт пока не подключён к отправке открытий и стартовому восстановлению; их локальный учёт требует отдельной атомарной реализации.


### ARCH403: атомарный локальный учёт открытия
settle_open записывает стадии открывающей заявки, ссылку на SandboxTrade и accounted_units в одной транзакции под блокировкой намерения. Средняя цена и первое время исполнения происходят из подтверждённых стадий; неизвестные комиссия/net не подменяются нулём. Повтор не добавляет позицию; частичное исполнение обновляет исходную. Изменённая история или локальная позиция блокируют перезапись. unresolved FIGI сохраняется до отдельного завершения runtime; этот компонент ещё не подключён к отправке/восстановлению.


### ARCH404: восстановление незавершённого открытия
Live startup вызывает _restore_open_intents после close recovery. Загруженный original request остаётся PENDING_RECONCILIATION и не отправляется повторно. _reconcile_durable_open сначала сохраняет наблюдение; подтверждённый нулевой отказ/отмена освобождает только собственную блокировку. Ненулевое исполнение учитывается settle_open, но блокировка остаётся до завершения runtime, ещё не реализованного. Полная политика выхода должна восстанавливаться из сохранённых параметров, не из нового config.


### ARCH405: версия и параметры политики выхода
ExitPolicySnapshot хранит точные constructor-параметры поддерживаемой политики, policy_id/version; ATR-кэш не сохраняется. OpenIntent v2 и метаданные локального открытия сохраняют snapshot. Version1 без политики читается как unknown. Восстановление типа/параметров проверено golden-примерами для обеих сторон; подключение этих данных к runtime-восстановлению памяти ещё требуется.


### ARCH406: завершение открывающей заявки
После записи стадий _finish_open_runtime сверяет брокерский остаток, восстанавливает оригинальную политику и защиту, проверяет риск и вызывает acknowledge_open. Последний атомарно проверяет неизменность journal/local provenance и записывает runtime_accounted_units. Слот освобождается только при полном исполнении либо подтверждённой отмене остатка. Частичное активное исполнение удерживает слот; отмена остатка перед защитным закрытием пока не реализована. Завершение открытий ещё не подключено к новой отправке adapter.


### ARCH407: защита завершённых позиций при старте
Live/sandbox восстановление использует только текущие mode/account и bound sessions. Требуются уникальная локальная открытая строка, совпадающий брокерский остаток и сохранённая политика. Текущие SL/TP и trailing_active берутся из этой строки; отключённый TP не пересчитывается. Плохая позиция не мешает восстановить другие, но блокирует новые входы. Ошибка получения портфеля не означает отсутствие позиций. Paper startup пока не переведён на этот контракт.


### ARCH408: отправка открытия через общий журнал
LiveBroker.open_position резервирует OpenIntent до SDK post и возвращает OpenSubmission, не цену исполнения. Runtime заранее помечает исходную заявку pending и после receipt выполняет подтверждённый стадиями учёт и сверку. Частичное исполнение и неизвестный исход удерживают reservation; повтор не отправляет второй ордер. Текущий durable open поддерживает только отсутствие плеча/маржинального согласия. PaperBroker сохраняет свой ценовой контракт. Отмена активного остатка до protective close ещё требуется.


### ARCH410 — cancellation request is not cancellation proof
The adapter cancellation method binds original mode/account/request ID and only dispatches a request. Neither success response nor timeout releases the opening reservation. CloseAfterOpen persists the first close reason on the original opening row and survives opening acknowledgement. Operational cancel/reconcile/close handoff and startup discovery remain unimplemented; these primitives alone do not fix protective closing of active partial openings. Isolated acceptance:22 checks passed; no production migration/API.


### ARCH411 — close-after-open recovery identity
Protective close blocked by a durable opening stores its first reason on that original scoped journal row. Recovery lookup must include released opening rows, because opening acknowledgement does not fulfill the exit request. The closing request UUID is deterministic from version/mode/account/original opening ID. Retiring a request requires proof from that closing journal, not opening release or a cancellation response. Loader and runtime trigger capture are tested; startup consumption and cancellation/handoff remain outstanding.


### ARCH412 — cancellation reconciliation and persistent exit pause
A saved close-after-open trigger now requests scoped cancellation and then reconciles authoritative order state even after cancel timeout. Racing fills are accounted before any future close. Opening acknowledgement only removes entry_reconciliation; close_after_open remains. Startup restores pending exit pauses including released openings; errors fail closed. Closing handoff/retirement and startup cancellation resumption are still outstanding, so this is not operational completion.


### ARCH413 — fulfilling the saved exit request
Recovery omits confirmed empty terminal openings and nonempty openings whose deterministic closing journal proves complete linked FILLED accounting/runtime acknowledgement. Original request remains as audit evidence. Partial closes never fulfill it; wrong links fail recovery. Matching scoped pause retirement follows successful lookup only. Tests seed completion journal states; end-to-end handoff proof is still outstanding.


### ARCH415 — trailing during partial opening
Late cumulative opening fills may preserve persisted active trailing with finite positive stop tightened relative to original directional stop and disabled TP. Original intent plan and prior stage proof must still match. Accounting updates quantities/prices/evidence while preserving protection and unrelated metadata. No arbitrary manual level changes or authenticated intermediate revision history is claimed. Golden PostgreSQL coverage includes both sides and actual runtime partial->persist stop->full completion.


### ARCH416 — close-after-open handoff
Saved exit requests are resumed after pending reconciliation. Preparing a close requires terminal released and fully acknowledged opening plus exact linked local position/provenance; fresh broker inventory must agree. Runtime submits original stable closing UUID through common adapter with typed entry proof. Existing closing reservation forbids resending; closing journal fulfillment retires only matching pause. Actual isolated adapter completion tested; full process-kill and cancelled partial-close remainder chaining remain unverified/unimplemented respectively.


### ARCH417 — handoff recovery acceptance boundary
Actual adapter with lost post response recovers via fresh runtime object and disposed/recreated DB pool: original closing request retained, stages account20, saved exit fulfills, total posts1. Two concurrent handoff invocations within one runtime also post once. Test broker reused; no OS process kill/full startup or cross-process race acceptance. Receipt ROOT/close_after_open_restart_r1.json.


### ARCH419 — deterministic remaining-quantity close chain
After positive fully accounted/runtime-acknowledged terminal partial cancellation, next closing UUID derives from original close UUID and cumulative closed units. Each link requests exactly remaining units and retains original entry row/request. Active, unacknowledged or zero-progress links cannot authorize another order. Total acknowledged closure fulfills saved exit; original opening evidence still prices residual basis. Isolated actual adapter20->10cancelled->10filled passes. Zero-fill retry policy and OS process lifecycle acceptance remain outstanding.

### Оценка капитала в подтверждённый период закрытия

В отдельном исследовательском сценарии `report_carried_marks=True` возвращает оценки по последним ценам и их возраст для отсутствующих котировок. `backend/reports/architect_20261115/closed_session_valuation.py` принимает явно подтверждённый интервал `[closed_from, closed_until)` и допустимый возраст каждой цены на его начало. Для всего портфеля интервал должен покрывать все инструменты, которым требуется перенос оценки. Пропуски внутри торгового периода остаются неизвестными. Сценарные оценки не передаются в допуск заявок или определение исполнения и публикуются отдельно от строгой кривой.

Контроль на октябрьском снимке VWAP использовал закрытие 24–25.10 МСК из файла `october_24_25_market_closed_evidence_r1.json` и исследовательский допуск 15 минут до начала закрытого интервала. Восстановлено 2 880 оценок, осталось 10 964 пропуска, наблюдаемая просадка 2,82%, полная неизвестна. Исходный отчёт совпал при повторе. Отдельный результат `october_vwap_closed_session_valuation_r1.json` фиксирует контрольные суммы.
