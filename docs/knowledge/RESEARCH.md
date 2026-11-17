# Исследовательский протокол

## Что ищем

Сочетания функций и нескольких таймфреймов, модели экономической доходности, дневной контекст и выбор акций. Набор существующих роботов не канонический: новые кандидаты допустимы как заранее ограниченные исследовательские гипотезы. Режим рынка — описательное измерение, не автоматическое предсказание прибыли; [ADR-0003](../adr/0003-regime-contract.md).

## Три независимые ветки

| Ветка | Полная цепочка внутри работника |
|---|---|
| B1 | Атомы → пары/тройки ТФ-признаков → замороженные правила → сделки |
| B2 | Net-return targets 30/60/120 минут → фиксированная матрица моделей → прогнозы → сделки |
| B3 | Контекст прошлых 20/60/120 сессий → 18 политик → отбор акций → дневные сделки |

Точные ограничения и файлы: [PARALLEL-DAY-1](../PARALLEL_DAY_1.md). Завершение одного работника не блокирует остальных. Эта база не заменяет задания и не расширяет их разрешения.

## Причинность и выбор

- Признак строится из доступного прошлого; текущий дневной close/объём не может участвовать в утреннем решении. Daily context — только по предыдущей закрытой сессии с явным source-end time.
- Join разметки — по устойчивому row_id/времени, не по enumerate после удаления строк.
- Label, качество будущего, MATURE/CENSORED, eligibility/identity и outcome metadata не входят в X. MATURE будущего не выбирает live inference строки.
- Transform/imputer/quantiles обучаются на train. Purge по фактическому label-end и embargo исключают пересечение границ.
- Для текущего пакета train до 2026-05-31, validation июнь–июль, eval 2026-08-01..2026-10-08. Этот eval уже использован в разработке, он **не нетронутый holdout** всего проекта.
- Выбор правила/модели/порога только по заранее объявленному критерию validation; заморозка до eval. Учитывать все попытки, отрицательные и NO_SELECTION.
- Отбор акций опирается на тогда известные данные. Нельзя выбирать пригодность по покрытию/PnL за весь будущий год.

## Что сдаётся

Manifest input/config/code hashes, границы train/val/eval, полный список попыток, freeze, правила или сохранённые model+transforms, predictions, signal outcomes, закрытый/открытый ledger, итоговый JSON и текстовое объяснение. Показывать gross/fees/slippage/net, cash/exposure, known/unknown, капитал, baseline, paired calendar, тикеры/месяцы, исключение лучшего тикера/дня и чувствительность к расходам.

Единица сравнения должна совпадать: event mean net, сумма сделок и доходность портфеля с ограниченным капиталом — разные метрики. Precision и доля правильных направлений сами по себе не показывают прибыль после расходов. Вероятность плюса не обязана быть 50%, величины выигрыша/проигрыша тоже различны.

## Новости и внешние индикаторы

Нефть, золото, валюты и новости — кандидаты контекста, а не готовые сигналы. Для истории важны publication/availability time, версии текста и отсутствие будущих правок; современная LLM-оценка старой новости не является эталонной доходностью. Эталон реакции определяется будущим окном и расходами в отдельном причинном эксперименте. Упоминание DeepSeek в MVP не доказывает историческую валидность интеграции и не разрешает внешний запрос.

Старые идеи собраны в каталоге как backlog. Перенос в execution path требует отдельного обоснованного этапа, не статуса «идея интересна».


### ARCH414 — C7 timing and input acceptance
A pure exact-label lookup does not prove late-arrival expiry: a cancelled signal must retain its terminal decision and the event scheduler must gate confirmation toT+5. Root probe found revival on same-label late arrival despite23 green goldens (the purported late-arrival test reused empty input). Manifest month/day grouping must explicitly use MSK boundaries, not session TimeZone; observed22:00UTC month boundaries conflict with frozen21:00UTC. Ten tickers with initial warmup does not justify dropping the remaining28 from the whole interval: eligibility must evolve causally with available history. Ordered candle-value fingerprints are required in addition to source/config hashes. Review receipt ROOT/C7_root_review_r1.json; evaluation not accepted yet.


### ARCH418 — C7 timing accepted narrowly
Independent synthetic review verifies WAIT before confirmation close and persistent cancellation after missed expiry, with invalid prices rejected. Coverage code now declares repeatable read/MSK/fingerprint, but output remains to review. Depth-ready time can fall off a15m boundary; actual eligibility must select a present closed15m bar with fresh higher-timeframe evidence. Retained CONFIRM is a decision record, not permission for repeated portfolio entries: caller must consume once. Review ROOT/C7_root_review_r2.json; evaluation not yet accepted.


### ARCH420 — database timezone versus returned timestamp representation
C7r2 groups on correct Moscow midnight but returned UTC-aware timestamps must explicitly astimezone(MSK) before month labels/ MSK serialization. Merely SET LOCAL TIME ZONE does not establish Python object display zone. Root audit maps eight blocks toJan-Aug2026 and sums6402486 evaluation rows. This certifies artifact arithmetic/time conversion only, not source completeness or independently reproduced input hash.
