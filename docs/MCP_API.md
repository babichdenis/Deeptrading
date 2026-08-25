# MCP_API.md — справочник API-эндпоинтов для ИИ-агента

> Это контракт для MCP/LLM: нейросеть управляет конфигурациями, очередью тестов
> и данными ЧЕРЕЗ ЭТИ ЭНДПОИНТЫ. Код движка трогать нельзя (см. MCP_idea.md).
> Базовый URL: `http://<host>:8000` , всё JSON.
> Время: ISO-8601, UTC (`Z`). Даты можно слать без зоны (`2026-07-01`) — API сам
> приведёт к UTC.

## Правила (must)
- Сигналы считаются **один раз и кэшируются** по (figi, интервал, стратегия, параметры, период).
- Период теста НЕ пересчитывает сигналы: стратегии считаются по полной истории,
  даты теста фильтруют только свечи прогона.
- Режим LIVE не существует — только paper.
- Каждый эксперимент/тест воспроизводим: engine_version + cost_model_pinned + конфиг-hash.

---

## 1. Свечи и данные

### POST /api/candles/{figi}/sync
Докачивает недостающие свечи в БД (кеш). Параметры:
`interval_name`, `days`, `from_ts`, `to_ts` (ISO).
```json
{"interval_name":"hour","days":120,"from_ts":"2026-07-01T00:00:00Z","to_ts":"2026-08-01T00:00:00Z"}
```
→ `{downloaded, cached_bars, requests, coverage_from, coverage_to}`

### GET /api/candles/{figi}/coverage
Покрытие по интервалам: `{intervals: {hour:{from,to,bars}, ...}}`

### GET /api/analysis/{figi}?interval_name=&limit=
Свечи + SMA20/EMA50/RSI/BB/MACD (для графиков).

---

## 2. Каталог

### GET /api/v1/strategies/catalog
Все стратегии: id, name, family, params_schema (min/max/default), волна.
Сейчас: rsi_reversal, bollinger_reclaim, pullback_ema, vwap_reclaim,
range_compression_breakout, macd_cross, donchian_breakout.

### GET /api/v1/policies/catalog
Exit-политики: fixed_sl_tp {stop_pct,target_pct}, atr_stop {period,multiplier,risk_reward},
atr_trailing {period,initial_stop_atr,activation_atr,trail_distance_atr}.

---

## 3. Сигналы (стратегии → raw signals)

### POST /api/v1/signals/compute
```json
{
  "figi":"BBG004730N88","interval_name":"hour","strategy_id":"rsi_reversal",
  "params":{"period":14,"oversold":35},"days":240,
  "from_ts":"2026-07-01T00:00:00Z","to_ts":null,"force":false
}
```
→ `{run_id, cached, count, signals:[{ts,side,status,reason,features}]}`
Кеш: тот же figi+интервал+стратегия+params+период → `cached:true`, 0 пересчёта.

### POST /api/v1/quorum/compute — кворум поверх готовых runs
```json
{"member_run_ids":["<run1>","<run2>"],"k":2}
```
→ кворумные сигналы (same-bar, window_bars=0) с составом голосов в features.

---

## 4. Конфигурации (Warehouse)

### POST /api/v1/warehouse/configurations — создать
```json
{
  "name":"RSI+BB 2of2 · hour",
  "interval_name":"hour",
  "members":[{"strategy_id":"rsi_reversal","params":{}},
             {"strategy_id":"bollinger_reclaim","params":{}}],
  "quorum":2,
  "exit_policy":{"id":"fixed_sl_tp","params":{"stop_pct":0.01,"target_pct":0.02}},
  "min_hold_bars":0, "allow_short":false,
  "session_policy":{"entry_cutoff_bars":0,"overnight":true},
  "figi":null
}
```
`stop_pct`/`target_pct` — **доли** (0.01 = 1%). → `{configuration_id, status:"DRAFT", ...}`

### GET /api/v1/warehouse/configurations — список (всех)
### GET /api/v1/warehouse/configurations/{id} — полная (с lab_result)
### PATCH /api/v1/warehouse/configurations/{id} — изменить (→ DRAFT)
### DELETE /api/v1/warehouse/configurations/{id} — удалить
### POST .../configurations/{id}/clone — копия (DRAFT)

---

## 5. Очередь тестов (Lab) — ГЛАВНОЕ для управления

### POST /api/v1/lab/queue — поставить на тест
```json
{
  "config_id":"<uuid>",
  "tickers":["SBER","GAZP","LKOH"],   // пустой [] = топ ликвидные
  "date_from":"2026-07-01",           // опционально
  "date_to":"2026-08-01",             // опционально
  "period_days":30
}
```
→ `{run_id, status:"QUEUED", queued:true}`

### GET /api/v1/lab/queue — вся очередь
```json
{"max_concurrent":1,"count":2,"runs":[
  {"run_id","config_id","config_name","members":[{strategy_id,params}],"quorum","exit_policy",
   "interval_name","min_hold_bars","allow_short","status","tickers",
   "date_from","date_to","period_days",
   "progress":{"done","total","current"},"error",
   "lab_result":{...},"created_at"}
]}
```
Статусы: `QUEUED | RUNNING | PAUSED | DONE | FAILED | CANCELLED`.

Диспетчер выполняет не больше `max_concurrent` тестов одновременно; завершился один → следующий.

### POST /api/v1/lab/queue/{run_id}/pause
RUNNING → прерывается, возвращается в QUEUED (прогресс теряется). QUEUED → PAUSED.
### POST /api/v1/lab/queue/{run_id}/resume — PAUSED → QUEUED
### POST /api/v1/lab/queue/{run_id}/stop — CANCELLED, конфигурация → DRAFT (в левую колонку)
### DELETE /api/v1/lab/runs/{run_id} — удалить запись теста
### POST /api/v1/lab/runs/{run_id}/recycle — удалить тест, конфигурация → DRAFT

### GET /api/v1/lab/settings
→ `{"max_concurrent_tests":1}`
### PUT /api/v1/lab/settings {"max_concurrent_tests":2}

---

## 6. Результаты теста

Итог лежит в `configuration.lab_result` (GET /warehouse/configurations/{id}):

```json
{
  "totals":{"stocks","trades","positive_stocks","total_net","wins","losses"},
  "per_stock":[{"ticker","mode":"long|short","active_days","wl":"6 (L-2 / S-4)",
                "wins_total","wins_l","wins_s","losses_total","trades","pnl":{total,pos,neg}}],
  "independent" ...
  "funnel":{"<strategy>:raw":{BUY,SELL}, "quorum_signals":N},   // "почему мало сделок"
  "curve":[{"time","equity"}],
  "by_day":[{"date","trades","gross","commission","net"}],
  "consecutive_losses":N,
  "top1_analysis":{"top1_figi","top1_net","net_without_top1","concentration_pct"},
  "trades":[{"side","mode","entry_time","entry_price","exit_time","exit_price",
             "gross_pnl","commission","net_pnl","exit_reason","ticker","entry_votes","entry_members"}],
  "elapsed_sec":N
}
```

- `wl` = «прибыльных (L-win / S-win)», счет сделок отдельно в `trades`.
- `mode long/short` — тест делится на два режима: в long-режиме входы только BUY (SELL закрывает),
  в short — только SELL (BUY закрывает). Это не дубликаты: одна позиция на фиги в каждый момент.

---

## 7. ML-фильтр сигналов (ML_idea.md)

### POST /api/v1/ml/train — обучить модель на сигналах стратегии
```json
{"figi":"BBG004730N88","interval_name":"hour","strategy_id":"rsi_reversal",
 "params":{},"days":240,"horizon_bars":20,"r_multiple":1.0}
```
Label: цена достигла +R раньше −R за `horizon_bars` (R = ATR). →
`{model_id, name, strategy_run_id, signals_used, metrics:{accuracy_train,accuracy_holdout,base_rate,...}}`
**Важно**: accuracy_holdout ≤ base_rate → модель не даёт преимущества.
### GET /api/v1/ml/models — список обученных
### POST /api/v1/ml/predict {"model_id","run_id":null,"threshold":0.5} — проскорить сигналы,
→ `{scored, accepted, rejected, predictions:[{ts,side,probability,decision}]}`
### GET /api/v1/ml/models/{model_id}/predictions

ML — **слой оценки сигналов**: предсказания ссылаются на signal_id, raw сигналы не меняются.

---

## 8. Бот (paper only)

### POST /api/v1/bot/start
```json
{"strategy_id":"rsi_reversal","params":{},"interval_name":"5min","top_n":6,
 "qty_per_trade":1,"stop_pct":0.01,"target_pct":0.02,"allow_short":false,"initial_cash":100000}
```
Сам выбирает топ-N волатильных по ATR%, греет буфер из БД, стрим = докачка закрытых баров.
### POST /api/v1/bot/stop · GET /api/v1/bot/status
### GET /api/v1/bot/positions · GET /api/v1/bot/trades
### POST /api/v1/bot/reset?initial_cash=100000
Живой бот НЕ запускается — только paper.

---

## 9. Эксперименты (быстрая проверка одной акции)

### POST /api/v1/experiments
```json
{"figi":"BBG004730N88","interval_name":"hour","days":240,
 "strategy_id":"rsi_reversal","params":{},
 "exit_policy":{"id":"atr_trailing","params":{"period":14,"initial_stop_atr":2,"activation_atr":1,"trail_distance_atr":2}},
 "ml_filter":{"model_id":"<uuid>","threshold":0.55},   // опционально
 "purpose":"DESIGN"}
```
Синхронная проверка одной акции/периода (в отличие от очереди Lab). См. «Мы ожидаем» ниже.

---

## 10. Тест: потолок торговли и ансамбль ролей (вкладка «Тест»)

Диагностические эндпоинты: «сколько максимум можно заработать» (оракул)
и «насколько ансамбль функций приближается к этому потолку».
Детерминированы: в `meta.request_hash` хэш запроса, setup-сигналы кэшируются
через /signals/compute. Никакого look-ahead: bias по предыдущему часу,
entry по прошлым барам, исполнение по open следующего бара.

### POST /api/v1/test/max-profit — потолок прибыли
```json
{"figi":"BBG008F2T3T2","interval_name":"1min","days":30,"limit":3000,
 "threshold_pct":0.5,"fee_rate_pct":0.05,"capital":100000}
```
→ `{ceiling_1lot, perfect_intraday, perfect_multiday, buy_hold, trades,
    equity, predictability:{confirm_lag_min_median, captured_median_pct,
    rules:[{name,precision,coverage,base}], walkforward:{entry,exit}}}`
- `ceiling_1lot` — абсолютный потолок (1 лот на каждом баре buy low / sell high);
- `perfect_*` — идеальный свинг-трейдер по подтверждённому зигзагу;
- `predictability` — доля сигналов, предсказуемых заранее индикаторами.

### POST /api/v1/test/ensemble — ансамбль ролей + режимы
```json
{
  "figi":"BBG008F2T3T2","days":3,"capital":100000,"lot":10,
  "use_all_setups":true, "drop_useless":true,
  "setups":[{"strategy_id":"rsi_reversal","tf":"5min","params":{}}],
  "quorum":2, "entry_window_min":15,
  "exit_policy":{"id":"atr_stop","params":{"period":14,"multiplier":2,"risk_reward":2}},
  "regime":{"tf":"5min"},
  "adaptive":[
    {"name":"HIGH_VOLATILITY","no_trade":true},
    {"name":"RANGE","config":{"mode":"both","exit_policy":{"id":"fixed_sl_tp","params":{"stop_pct":1,"target_pct":2}}}}
  ],
  "oracle":{"threshold_pct":0.5,"fee_rate_pct":0.05}
}
```
Pipeline: bias (1h EMA) → setups (любые функции каталога на своих ТФ) + quorum
(same-bar) → entry (1m micro breakout, close>high[N]) → движок со стопами →
сравнение с оракулом (зигзаг на том же периоде, фиксированный размер позиции).

→ ответ:
```json
{
  "meta": {"engine_version":"ensemble_v2","request_hash":"...","qty_shares":4000,"params":{...}},
  "regime": {"tf":"5min","timeline":[{"from","to","state","reason"}]},
  "oracle": {"swings","trades","gross","net","zones":[{from,to}]},
  "static": {
    "funnel": {"raw_signals","unique_points","entries_raw","entries_accepted","entries_rejected","trades"},
    "setups": {"<strategy_id>":{"signals","BUY","SELL"}},
    "quorum_list": [{"ts","side","votes"}],
    "entries": [{"ts","side"}], "rejected": [{"ts","side","reason"}],
    "trades": [{"side","regime","entry_ts","exit_ts","entry_px","exit_px","stop","target",
                "gross","commission","net","exit_reason","mfe_r","mae_r"}],
    "economic": {"trades","gross","commission","slippage","net","profit_factor","win_rate_pct","avg_hold_bars","turnover","equity"},
    "capture_ratio": {"causal_gross_oracle_gross_pct","causal_net_oracle_gross_pct"},
    "quality": [{"role","strategy_id","side","signals","coverage_pct","precision_pct","lead_min_median","false_positives","useless"}],
    "useless_strategies": [],
    "per_regime": {"RANGE":{"trades","gross","net","win_rate_pct"}},
    "movement_capture": {"mfe_r_median","mae_r_median","hit_1r_before_minus1r","hit_2r_before_minus1r"}
  },
  "adaptive": {...та же схема, если передан "adaptive" ...} | null,
  "comparison": {"static_net","adaptive_net","static_trades","adaptive_trades",
                 "static_capture","adaptive_capture"}
}
```

Правила интерпретации:
- `quality[].useless` / `useless_strategies` — функции с coverage=0 (бесполезные),
  `drop_useless:true` убирает их из набора до прогона;
- `capture_ratio` — доля, которую ансамбль ловит от оракула (диагностика, не прогноз);
- `adaptive` — конфигурации {режим → mode/no_trade/exit_policy}; режим считается
  по ЗАКРЫТЫМ барам (без look-ahead);
- `mfe_r/mae_r` — максимальный благоприятный/неблагоприятный ход в R;
- `same_side_reentry_cooldown_bars` — запрет повторного входа в ту же сторону
  после выхода (разворот разрешён сразу); `exit_confirm_window_bars` — выход
  только после повторного противоположного сигнала в окне;
- `counterfactual_reentries` — аудит отклонённых повторных входов: что было бы,
  если бы cooldown их пропустил (вход по open следующего бара, та же exit-политика).

Статус: PREVIEW-диагностика на коротком периоде, не является backtest-доказательством.

### POST /api/v1/test/ensemble-sweep — матрица FIGI × cooldown × confirmed exit
```json
{"figis":["BBG008F2T3T2","BBG004731032"],"days":3,"capital":100000,"lot":10,
 "price_min":1,"price_max":1000,"cooldowns":[0,5,15,30,60],"exit_confirms":[0,3],
 "use_all_setups":true,"drop_useless":true,"quorum":2}
```
- Цена акции берётся из последней закрытой 1m свечи; figi вне `[price_min, price_max]`
  отбрасываются (`filtered_out` с причиной) — чтобы не сравнивать net «ценой 100 и 5000»;
- net по одинаковому капиталу на позицию (позиция нормируется размером актива);
- cooldown задаётся в барах; на 1m `N баров = N минут` (в ответе `tf_minutes`);
- каждая ячейка: trades, episodes, gross, costs, net, pf, win_rate, break_even_median,
  reentry_rejected, counterfactual {n, mean_net, median_net, wins_pct}.

---

## Рекомендуемые последовательности (workflow)

### A. Быстрая проверка одной идеи
1. `POST /signals/compute` (или советую: если стратегия известна — `POST /experiments` сразу)
2. `POST /experiments` → метрики одной акции

### B. Полный конвейер (рекомендуется)
1. `GET /strategies/catalog` — выбрать стратегии
2. `POST /warehouse/configurations` — собрать конфигурацию (пак + кворум + выходы + TF)
3. `GET /warehouse/configurations` — взять configuration_id
4. `POST /lab/queue` — поставить на пул
5. поллинг `GET /lab/queue` до `status: DONE` (прогресс в `progress`)
6. `GET /warehouse/configurations/{id}` — итоги (`lab_result.totals/funnel/trades`)
7. решить: `POST /lab/runs/{run_id}/recycle` (плохо) или оставить (хорошо)
8. `POST /ml/train` + `POST /ml/predict` — проверить ML-фильтр
9. снова `POST /lab/queue` с теми же членами + порог ML (эксперимент «без/с фильтром»)

### C. Отбор акций для теста
Ликвидные: SBER, GAZP, LKOH, GMKN, ROSN, MTSS, TATN, MGNT, CHMF, ALRS, PLZL, SNGS (top_n).
Или `tickers: []` — бэк возьмёт топ ликвидных.

---

## Дисциплина (из MCP_idea.md / main_plan.md)
- Не пересчитывать стратегии при смене SL/TP/кворума — сигналы кэшируются.
- `DESIGN` для исследования, `VAL` — отдельным прогоном на другой выборке, не по одному Валу.
- Не подгонять параметры по одному периоду; проверять H1/H2 (в lab_result.halves),
  концентрацию (top1_analysis), стабильность по акциям.
- Считать число прогонов (trials) — доля случайно удачных результатов.