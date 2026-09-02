# Task: IMOEX trend regime & strength attribution (shadow)

## Goal

Измерить, в каком тренде (up/down/flat) и с какой силой тренда
находился IMOEX в моменты входов/выходов оракула (July 2026).

Не менять:
- EngineRunner
- entry logic
- exit policy
- quorum / cooldown / session policy
- CostModel / sizing / ML
- фактические trade results

Использовать только:
- canonical baseline runs (July 2026)
- IMOEX INDEXCF 1m свечи (point-in-time)
- rolling volatility IMOEX (point-in-time)

## Data

Вход:
- trades.csv (July 2026, 517 сделок)
- IMOEX INDEXCF 1m свечи (point-in-time, без look-ahead / утечек)
- rolling volatility IMOEX (например 5m/30m ATR или std)

## Method

### 1. IMOEX trend regime

Построить три независимых определения тренда IMOEX:

**A. EMA-slope**

```text
EMA_fast = EMA(close, 20)
EMA_slow = EMA(close, 50)

diff = EMA_fast - EMA_slow
diff_norm = diff / close

if diff_norm > +τ → up_trend
if diff_norm < -τ → down_trend
else → flat
```

τ = 0,0015 (0,15%), можно варьировать 0,001–0,002.

**B. ADX**

```text
ADX(14) > 25 → тренд
  +DI > -DI → up_trend
  -DI > +DI → down_trend
ADX ≤ 25 → flat
```

**C. Linear regression slope**

```text
slope = slope линейной регрессии close за последние 40 баров
slope_norm = slope / close

if slope_norm > +τ → up_trend
if slope_norm < -τ → down_trend
else → flat
```

τ = 0,0005–0,001.

Для каждого метода сохранить:
- trend_regime ∈ {up_trend, down_trend, flat}
- trend_raw_strength (diff_norm / ADX / slope_norm)

Нормировать strength на rolling volatility IMOEX:

```text
vol_imoex = rolling_std(return_1m, 30)
trend_strength_norm = trend_raw_strength / vol_imoex
```

### 2. Привязка к входам/выходам

Для каждой сделки из `trades.csv`:

```text
entry_time
exit_time
side ∈ {LONG, SHORT}
entry_price
exit_price
net
exit_type ∈ {target, stop_loss, signal_exit}
```

Определить для каждого из трёх методов (A/B/C):

```text
entry_trend_regime
entry_trend_strength_norm

exit_trend_regime
exit_trend_strength_norm

hold_trend_regime = mode(trend_regime) за [entry_time, exit_time]
hold_trend_strength_mean = mean(trend_strength_norm) за [entry_time, exit_time]
```

### 3. Группировки

#### A. По режиму тренда на входе

Группы:

```text
LONG & entry_trend_regime = up_trend
LONG & entry_trend_regime = down_trend
LONG & entry_trend_regime = flat

SHORT & entry_trend_regime = up_trend
SHORT & entry_trend_regime = down_trend
SHORT & entry_trend_regime = flat
```

Для каждой группы посчитать:

```text
trades count
gross
commission
slippage
net
net/trade
PF
win rate
target/stop/signal exit mix
MFE
MAE
hold time
MTM DD contribution
```

#### B. По силе тренда на входе

Разбить `entry_trend_strength_norm` на 3–4 квантиля:

```text
Q1: weak trend
Q2: medium trend
Q3: strong trend
(опционально Q4: very strong)
```

Сравнить те же метрики по квантилям.

#### C. По согласованности side × trend

```text
aligned_trend:
  LONG & up_trend
  SHORT & down_trend

counter_trend:
  LONG & down_trend
  SHORT & up_trend

flat:
  LONG/SHORT & flat
```

Сравнить:

```text
net/trade
PF
win rate
```

#### D. По режиму на выходе

Аналогично, но по `exit_trend_regime` и `exit_trend_strength_norm`:

```text
exit в up_trend
exit в down_trend
exit в flat
```

Посмотреть, отличается ли экономика target/stop/signal в разных режимах.

#### E. По смене режима за время удержания

Например:

```text
entry up_trend → exit flat
entry up_trend → exit down_trend
entry flat → exit up_trend
...
```

И сравнить P&L для разных переходов.

### 4. Сравнение методов тренда

Сравнить A/B/C по:
- распределению regime (up/down/flat %)
- стабильности разделения net/trade между группами
- correlation между методами

Выбрать один preferred method для будущих экспериментов.

## Output

- reports/{run_id}/imoex_trend_attribution.json
  - summary stats по regime/strength
  - breakdown by group (A–E)
  - method comparison (A/B/C)
- reports/{run_id}/imoex_trend_trade_audit.csv
  - per-trade:
    - entry/exit trend_regime (A/B/C)
    - entry/exit trend_strength_norm
    - hold_trend_regime/strength
    - actual net
- reports/{run_id}/imoex_trend_methodology.md
  - target definition
  - sample counts
  - point-in-time checks
  - limitations
  - preferred method recommendation

## Tests

- все feature values point-in-time
- no oracle data
- no EngineRunner decision changes
- end-of-data handling корректна
- trend_regime/strength определены до entry/exit времени

## Conclusion

Одно из:
- SUPPORTED_FOR_FUTURE_EXPERIMENT
- INCONCLUSIVE
- REJECTED
