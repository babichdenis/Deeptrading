# EXPERIMENTS — Wave A: порты OsEngine

> Протокол карточек: `docs/osengine/PORTING_MAP.md` #44–#46. Каждому конфигу присвоен уникальный ID EXP-0000XX.
> Харнесс: `backend/scripts/bt_ose_sweep.py` — детерминированная серия `ose.metrics.synthetic_series(n=400)`, seeds [42, 7, 123], объём 1.
> COMMISSION=0, SLIPPAGE=0: каркас Wave A не моделирует издержки — поля по протоколу оставлены, заполнятся на реальном датасете.
> AVG WIN/AVG LOSS, HOLD, LONG/SHORT split: харнесс не трекает — n/a, следующая волна.
> Юнит-тесты: `backend/tests/test_ose_{indicators,metrics,robots,strategy}.py` — 90 passed.

Машина: `MacBook-Pro-Babich.local` · Python 3.11.7 · git `2a47fe6` · code_sha256 `45aee2f66cc8a0d6`
Сверка машин: WIN8 (Windows 10, Python 3.12) — **72/72 прогонов бит-в-бит** (метрики + equity-хэши).
Отчёты: `backend/reports/bt_ose_sweep_mac_fix.json` и `bt_ose_sweep_win8_fix.json` (на .8: C:/Users/nadts/dt-ose-work/reports/).

## Сводная таблица

| EXP | Robot | Params | NET(mean) | PF(mean) | MAX DD | Trades | WR | STATUS |
|---|---|---|---:|---:|---:|---:|---:|---|
| EXP-000001 | Bollinger | `default` | -132.5 | 0.00 | 132.5 | 9.0 | 0.0% | REJECT |
| EXP-000002 | Bollinger | `{"boll_deviation":2.5}` | -77.4 | 0.00 | 78.1 | 5.7 | 0.0% | REJECT |
| EXP-000003 | Bollinger | `{"boll_length":15}` | -152.4 | 0.00 | 152.4 | 9.7 | 0.0% | REJECT |
| EXP-000004 | Bollinger | `{"regime":"OnlyShort"}` | -64.5 | 0.00 | 64.5 | 4.3 | 0.0% | REJECT |
| EXP-000005 | EnvelopTrend | `default` | +195.2 | 186.27 | 2.4 | 30.3 | 59.7% | CANDIDATE |
| EXP-000006 | EnvelopTrend | `{"deviation":0.5}` | +193.5 | 215.81 | 2.4 | 35.3 | 64.2% | CANDIDATE |
| EXP-000007 | EnvelopTrend | `{"deviation":1.0,"trail_stop":1.0}` | +183.3 | 84.04 | 2.8 | 15.3 | 71.0% | CANDIDATE |
| EXP-000008 | EnvelopTrend | `{"trail_stop":0.5}` | +191.6 | 56.68 | 2.7 | 17.7 | 59.3% | CANDIDATE |
| EXP-000009 | PriceChannelTrade | `default` | +107.1 | ∞* | 7.3 | 10.0 | 93.3% | CANDIDATE |
| EXP-000010 | PriceChannelTrade | `{"length_down":10,"length_up":10}` | +170.7 | ∞ | 3.7 | 10.0 | 100.0% | CANDIDATE |
| EXP-000011 | PriceChannelTrade | `{"length_down":34,"length_up":34}` | +4.6 | 1.09 | 13.7 | 10.0 | 53.3% | WATCH |
| EXP-000012 | PriceChannelTrade | `{"regime":"OnlyLong"}` | +51.2 | ∞* | 6.9 | 5.0 | 86.7% | CANDIDATE |
| EXP-000013 | RsiContrtrend | `default` | +0.0 | 0.00 | 0.0 | 0.0 | 0.0% | REJECT |
| EXP-000014 | RsiContrtrend | `{"downline":30.0,"upline":70.0}` | +0.0 | 0.00 | 0.0 | 0.0 | 0.0% | REJECT |
| EXP-000015 | RsiContrtrend | `{"regime":"OnlyLong"}` | +0.0 | 0.00 | 0.0 | 0.0 | 0.0% | REJECT |
| EXP-000016 | RsiContrtrend | `{"rsi_length":14}` | +0.0 | 0.00 | 0.0 | 0.0 | 0.0% | REJECT |
| EXP-000017 | RsiTrade | `default` | +119.7 | ∞ | 5.8 | 9.0 | 100.0% | CANDIDATE |
| EXP-000018 | RsiTrade | `{"downline":30.0,"upline":70.0}` | +131.9 | ∞ | 5.9 | 9.0 | 100.0% | CANDIDATE |
| EXP-000019 | RsiTrade | `{"downline":40.0,"upline":60.0}` | +97.4 | ∞ | 7.3 | 9.0 | 100.0% | CANDIDATE |
| EXP-000020 | RsiTrade | `{"rsi_length":14}` | +134.9 | ∞ | 9.0 | 9.0 | 100.0% | CANDIDATE |
| EXP-000021 | SmaStoch | `{"regime":"OnlyShort","step":1.0}` | +0.0 | 0.00 | 0.0 | 0.0 | 0.0% | REJECT |
| EXP-000022 | SmaStoch | `{"step":0.5}` | +4.1 | ∞* | 1.8 | 1.0 | 33.3% | WATCH |
| EXP-000023 | SmaStoch | `{"step":1.0}` | -0.9 | 0.00 | 0.9 | 0.3 | 0.0% | WATCH |
| EXP-000024 | SmaStoch | `{"step":2.0}` | +0.0 | 0.00 | 0.0 | 0.0 | 0.0% | REJECT |

Статусы: CANDIDATE — net ≥ +20 и PF ≥ 1.5; WATCH — промежуточные; REJECT — минус или нет входов. Baseline для DELTA внутри семьи робота — его default-конфиг ({}), mean по 3 seedам.

---

## EXP-000001 — Bollinger · `default`

- **Robot**: Bollinger (`bollinger`)
- **Entry TF**: synthetic 1-bar (детерминированная серия, 400 баров)
- **Filter TF / Execution TF**: none
- **Exit**: логика порта (см. robots.py); trail_stop если задан в params
- **Dataset**: synthetic_series(n=400) × seeds [42, 7, 123]
- **Universe**: synthetic (1 инструмент)
- **Commission**: 0 · **Slippage**: 0

### Результат (mean по 3 seedам)

```text
TRADES: 9.0
WIN RATE: 0.0%
NET: -132.5   (GROSS/realized -124.4, OPEN/unrealized -8.1)
PF: 0.00
EXPECTANCY: -13.8
AVG WIN / AVG LOSS: n/a (Wave A не разделяет)
MAX DD: 132.5
COMMISSION: 0   SLIPPAGE: 0
AVG HOLD / MEDIAN HOLD: n/a (Wave A не трекает)
IN MARKET: 73.0%   BUY&HOLD: +6.8
```

### По seedам

| seed | trades | wr | net | realized | unrealized | max_dd | pf | buy&hold | equity_sha256 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 7 | 10 | 0.0% | -141.0 | -132.3 | -8.7 | 140.99 | 0.00 | +5.8 | `a1acdd81` |
| 42 | 9 | 0.0% | -138.3 | -132.8 | -5.5 | 138.29 | 0.00 | +19.8 | `f0ed687c` |
| 123 | 8 | 0.0% | -118.3 | -108.1 | -10.2 | 118.31 | 0.00 | -5.3 | `aba0768d` |

### DELTA vs baseline — Bollinger `default ({})` (mean)

```text
baseline сам на себя (эталон семьи) — дельты 0.
Δ Net: +0.0
Δ PF: +0.00
Δ DD: +0.0
Δ Expectancy: +0.0
Δ Trades: +0.0
Δ WR: +0.0
Δ Commission / Δ Slippage: 0 / 0 (издержки не моделируются)
```

**STATUS:** REJECT

---

## EXP-000002 — Bollinger · `{"boll_deviation":2.5}`

- **Robot**: Bollinger (`bollinger`)
- **Entry TF**: synthetic 1-bar (детерминированная серия, 400 баров)
- **Filter TF / Execution TF**: none
- **Exit**: логика порта (см. robots.py); trail_stop если задан в params
- **Dataset**: synthetic_series(n=400) × seeds [42, 7, 123]
- **Universe**: synthetic (1 инструмент)
- **Commission**: 0 · **Slippage**: 0

### Результат (mean по 3 seedам)

```text
TRADES: 5.7
WIN RATE: 0.0%
NET: -77.4   (GROSS/realized -71.7, OPEN/unrealized -5.6)
PF: 0.00
EXPECTANCY: -12.8
AVG WIN / AVG LOSS: n/a (Wave A не разделяет)
MAX DD: 78.1
COMMISSION: 0   SLIPPAGE: 0
AVG HOLD / MEDIAN HOLD: n/a (Wave A не трекает)
IN MARKET: 44.2%   BUY&HOLD: +6.8
```

### По seedам

| seed | trades | wr | net | realized | unrealized | max_dd | pf | buy&hold | equity_sha256 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 7 | 5 | 0.0% | -72.1 | -64.5 | -7.6 | 72.08 | 0.00 | +5.8 | `f1715f85` |
| 42 | 5 | 0.0% | -68.3 | -68.3 | +0.0 | 70.50 | 0.00 | +19.8 | `cfc87af1` |
| 123 | 7 | 0.0% | -91.7 | -82.4 | -9.3 | 91.71 | 0.00 | -5.3 | `b2381b64` |

### DELTA vs baseline — Bollinger `default ({})` (mean)

```text
Δ Net: +55.2
Δ PF: +0.00
Δ DD: -54.4
Δ Expectancy: +1.1
Δ Trades: -3.3
Δ WR: +0.0
Δ Commission / Δ Slippage: 0 / 0 (издержки не моделируются)
```

**STATUS:** REJECT

---

## EXP-000003 — Bollinger · `{"boll_length":15}`

- **Robot**: Bollinger (`bollinger`)
- **Entry TF**: synthetic 1-bar (детерминированная серия, 400 баров)
- **Filter TF / Execution TF**: none
- **Exit**: логика порта (см. robots.py); trail_stop если задан в params
- **Dataset**: synthetic_series(n=400) × seeds [42, 7, 123]
- **Universe**: synthetic (1 инструмент)
- **Commission**: 0 · **Slippage**: 0

### Результат (mean по 3 seedам)

```text
TRADES: 9.7
WIN RATE: 0.0%
NET: -152.4   (GROSS/realized -142.9, OPEN/unrealized -9.4)
PF: 0.00
EXPECTANCY: -14.8
AVG WIN / AVG LOSS: n/a (Wave A не разделяет)
MAX DD: 152.4
COMMISSION: 0   SLIPPAGE: 0
AVG HOLD / MEDIAN HOLD: n/a (Wave A не трекает)
IN MARKET: 82.7%   BUY&HOLD: +6.8
```

### По seedам

| seed | trades | wr | net | realized | unrealized | max_dd | pf | buy&hold | equity_sha256 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 7 | 10 | 0.0% | -160.4 | -148.9 | -11.5 | 160.36 | 0.00 | +5.8 | `aec0b40d` |
| 42 | 9 | 0.0% | -149.4 | -142.8 | -6.6 | 149.55 | 0.00 | +19.8 | `e14b4021` |
| 123 | 10 | 0.0% | -147.3 | -137.0 | -10.2 | 147.28 | 0.00 | -5.3 | `d984b9d3` |

### DELTA vs baseline — Bollinger `default ({})` (mean)

```text
Δ Net: -19.8
Δ PF: +0.00
Δ DD: +19.9
Δ Expectancy: -1.0
Δ Trades: +0.7
Δ WR: +0.0
Δ Commission / Δ Slippage: 0 / 0 (издержки не моделируются)
```

**STATUS:** REJECT

---

## EXP-000004 — Bollinger · `{"regime":"OnlyShort"}`

- **Robot**: Bollinger (`bollinger`)
- **Entry TF**: synthetic 1-bar (детерминированная серия, 400 баров)
- **Filter TF / Execution TF**: none
- **Exit**: логика порта (см. robots.py); trail_stop если задан в params
- **Dataset**: synthetic_series(n=400) × seeds [42, 7, 123]
- **Universe**: synthetic (1 инструмент)
- **Commission**: 0 · **Slippage**: 0

### Результат (mean по 3 seedам)

```text
TRADES: 4.3
WIN RATE: 0.0%
NET: -64.5   (GROSS/realized -56.3, OPEN/unrealized -8.1)
PF: 0.00
EXPECTANCY: -13.1
AVG WIN / AVG LOSS: n/a (Wave A не разделяет)
MAX DD: 64.5
COMMISSION: 0   SLIPPAGE: 0
AVG HOLD / MEDIAN HOLD: n/a (Wave A не трекает)
IN MARKET: 36.8%   BUY&HOLD: +6.8
```

### По seedам

| seed | trades | wr | net | realized | unrealized | max_dd | pf | buy&hold | equity_sha256 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 7 | 5 | 0.0% | -65.2 | -56.5 | -8.7 | 65.16 | 0.00 | +5.8 | `3bca95c8` |
| 42 | 4 | 0.0% | -68.8 | -63.3 | -5.5 | 68.82 | 0.00 | +19.8 | `3e100691` |
| 123 | 4 | 0.0% | -59.5 | -49.3 | -10.2 | 59.50 | 0.00 | -5.3 | `5888936c` |

### DELTA vs baseline — Bollinger `default ({})` (mean)

```text
Δ Net: +68.0
Δ PF: +0.00
Δ DD: -68.0
Δ Expectancy: +0.7
Δ Trades: -4.7
Δ WR: +0.0
Δ Commission / Δ Slippage: 0 / 0 (издержки не моделируются)
```

**STATUS:** REJECT

---

## EXP-000005 — EnvelopTrend · `default`

- **Robot**: EnvelopTrend (`envelop_trend`)
- **Entry TF**: synthetic 1-bar (детерминированная серия, 400 баров)
- **Filter TF / Execution TF**: none
- **Exit**: логика порта (см. robots.py); trail_stop если задан в params
- **Dataset**: synthetic_series(n=400) × seeds [42, 7, 123]
- **Universe**: synthetic (1 инструмент)
- **Commission**: 0 · **Slippage**: 0

### Результат (mean по 3 seedам)

```text
TRADES: 30.3
WIN RATE: 59.7%
NET: +195.2   (GROSS/realized +183.8, OPEN/unrealized +11.4)
PF: 186.27
EXPECTANCY: +6.2
AVG WIN / AVG LOSS: n/a (Wave A не разделяет)
MAX DD: 2.4
COMMISSION: 0   SLIPPAGE: 0
AVG HOLD / MEDIAN HOLD: n/a (Wave A не трекает)
IN MARKET: 89.3%   BUY&HOLD: +6.8
```

### По seedам

| seed | trades | wr | net | realized | unrealized | max_dd | pf | buy&hold | equity_sha256 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 7 | 25 | 68.0% | +200.3 | +186.2 | +14.1 | 2.30 | 258.84 | +5.8 | `01be7855` |
| 42 | 30 | 50.0% | +204.8 | +195.9 | +8.9 | 2.68 | 125.19 | +19.8 | `064a681a` |
| 123 | 36 | 61.1% | +180.5 | +169.3 | +11.2 | 2.20 | 174.77 | -5.3 | `ed974cb4` |

### DELTA vs baseline — EnvelopTrend `default ({})` (mean)

```text
baseline сам на себя (эталон семьи) — дельты 0.
Δ Net: +0.0
Δ PF: +0.00
Δ DD: +0.0
Δ Expectancy: +0.0
Δ Trades: +0.0
Δ WR: +0.0
Δ Commission / Δ Slippage: 0 / 0 (издержки не моделируются)
```

**STATUS:** CANDIDATE

---

## EXP-000006 — EnvelopTrend · `{"deviation":0.5}`

- **Robot**: EnvelopTrend (`envelop_trend`)
- **Entry TF**: synthetic 1-bar (детерминированная серия, 400 баров)
- **Filter TF / Execution TF**: none
- **Exit**: логика порта (см. robots.py); trail_stop если задан в params
- **Dataset**: synthetic_series(n=400) × seeds [42, 7, 123]
- **Universe**: synthetic (1 инструмент)
- **Commission**: 0 · **Slippage**: 0

### Результат (mean по 3 seedам)

```text
TRADES: 35.3
WIN RATE: 64.2%
NET: +193.5   (GROSS/realized +182.3, OPEN/unrealized +11.2)
PF: 215.81
EXPECTANCY: +5.3
AVG WIN / AVG LOSS: n/a (Wave A не разделяет)
MAX DD: 2.4
COMMISSION: 0   SLIPPAGE: 0
AVG HOLD / MEDIAN HOLD: n/a (Wave A не трекает)
IN MARKET: 87.8%   BUY&HOLD: +6.8
```

### По seedам

| seed | trades | wr | net | realized | unrealized | max_dd | pf | buy&hold | equity_sha256 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 7 | 30 | 70.0% | +198.5 | +184.6 | +13.9 | 2.30 | 346.89 | +5.8 | `8ba7e6f5` |
| 42 | 33 | 57.6% | +203.7 | +194.9 | +8.7 | 2.68 | 140.69 | +19.8 | `dbb4f34b` |
| 123 | 43 | 65.1% | +178.4 | +167.3 | +11.0 | 2.20 | 159.84 | -5.3 | `dce2e51d` |

### DELTA vs baseline — EnvelopTrend `default ({})` (mean)

```text
Δ Net: -1.7
Δ PF: +29.54
Δ DD: +0.0
Δ Expectancy: -0.9
Δ Trades: +5.0
Δ WR: +4.5
Δ Commission / Δ Slippage: 0 / 0 (издержки не моделируются)
```

**STATUS:** CANDIDATE

---

## EXP-000007 — EnvelopTrend · `{"deviation":1.0,"trail_stop":1.0}`

- **Robot**: EnvelopTrend (`envelop_trend`)
- **Entry TF**: synthetic 1-bar (детерминированная серия, 400 баров)
- **Filter TF / Execution TF**: none
- **Exit**: логика порта (см. robots.py); trail_stop если задан в params
- **Dataset**: synthetic_series(n=400) × seeds [42, 7, 123]
- **Universe**: synthetic (1 инструмент)
- **Commission**: 0 · **Slippage**: 0

### Результат (mean по 3 seedам)

```text
TRADES: 15.3
WIN RATE: 71.0%
NET: +183.3   (GROSS/realized +172.5, OPEN/unrealized +10.8)
PF: 84.04
EXPECTANCY: +12.0
AVG WIN / AVG LOSS: n/a (Wave A не разделяет)
MAX DD: 2.8
COMMISSION: 0   SLIPPAGE: 0
AVG HOLD / MEDIAN HOLD: n/a (Wave A не трекает)
IN MARKET: 88.9%   BUY&HOLD: +6.8
```

### По seedам

| seed | trades | wr | net | realized | unrealized | max_dd | pf | buy&hold | equity_sha256 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 7 | 14 | 71.4% | +188.4 | +174.9 | +13.5 | 2.74 | 50.42 | +5.8 | `2884f91c` |
| 42 | 12 | 91.7% | +195.4 | +187.2 | +8.2 | 2.03 | 176.71 | +19.8 | `c402c346` |
| 123 | 20 | 50.0% | +166.0 | +155.4 | +10.6 | 3.59 | 24.99 | -5.3 | `4d246bcb` |

### DELTA vs baseline — EnvelopTrend `default ({})` (mean)

```text
Δ Net: -11.9
Δ PF: -102.23
Δ DD: +0.4
Δ Expectancy: +5.7
Δ Trades: -15.0
Δ WR: +11.3
Δ Commission / Δ Slippage: 0 / 0 (издержки не моделируются)
```

**STATUS:** CANDIDATE

---

## EXP-000008 — EnvelopTrend · `{"trail_stop":0.5}`

- **Robot**: EnvelopTrend (`envelop_trend`)
- **Entry TF**: synthetic 1-bar (детерминированная серия, 400 баров)
- **Filter TF / Execution TF**: none
- **Exit**: логика порта (см. robots.py); trail_stop если задан в params
- **Dataset**: synthetic_series(n=400) × seeds [42, 7, 123]
- **Universe**: synthetic (1 инструмент)
- **Commission**: 0 · **Slippage**: 0

### Результат (mean по 3 seedам)

```text
TRADES: 17.7
WIN RATE: 59.3%
NET: +191.6   (GROSS/realized +180.2, OPEN/unrealized +11.4)
PF: 56.68
EXPECTANCY: +10.5
AVG WIN / AVG LOSS: n/a (Wave A не разделяет)
MAX DD: 2.7
COMMISSION: 0   SLIPPAGE: 0
AVG HOLD / MEDIAN HOLD: n/a (Wave A не трекает)
IN MARKET: 92.6%   BUY&HOLD: +6.8
```

### По seedам

| seed | trades | wr | net | realized | unrealized | max_dd | pf | buy&hold | equity_sha256 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 7 | 17 | 58.8% | +195.7 | +181.5 | +14.1 | 2.80 | 57.28 | +5.8 | `c3415cc8` |
| 42 | 15 | 66.7% | +202.1 | +193.2 | +8.9 | 2.98 | 71.12 | +19.8 | `701697ff` |
| 123 | 21 | 52.4% | +177.0 | +165.8 | +11.2 | 2.40 | 41.66 | -5.3 | `84f1ae0d` |

### DELTA vs baseline — EnvelopTrend `default ({})` (mean)

```text
Δ Net: -3.6
Δ PF: -129.58
Δ DD: +0.3
Δ Expectancy: +4.3
Δ Trades: -12.7
Δ WR: -0.4
Δ Commission / Δ Slippage: 0 / 0 (издержки не моделируются)
```

**STATUS:** CANDIDATE

---

## EXP-000009 — PriceChannelTrade · `default`

- **Robot**: PriceChannelTrade (`price_channel`)
- **Entry TF**: synthetic 1-bar (детерминированная серия, 400 баров)
- **Filter TF / Execution TF**: none
- **Exit**: логика порта (см. robots.py); trail_stop если задан в params
- **Dataset**: synthetic_series(n=400) × seeds [42, 7, 123]
- **Universe**: synthetic (1 инструмент)
- **Commission**: 0 · **Slippage**: 0

### Результат (mean по 3 seedам)

```text
TRADES: 10.0
WIN RATE: 93.3%
NET: +107.1   (GROSS/realized +99.2, OPEN/unrealized +7.9)
PF: ∞*
EXPECTANCY: +9.9
AVG WIN / AVG LOSS: n/a (Wave A не разделяет)
MAX DD: 7.3
COMMISSION: 0   SLIPPAGE: 0
AVG HOLD / MEDIAN HOLD: n/a (Wave A не трекает)
IN MARKET: 94.2%   BUY&HOLD: +6.8
```

### По seedам

| seed | trades | wr | net | realized | unrealized | max_dd | pf | buy&hold | equity_sha256 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 7 | 10 | 90.0% | +106.3 | +97.6 | +8.7 | 6.94 | 218.73 | +5.8 | `1413e02a` |
| 42 | 10 | 100.0% | +112.1 | +106.6 | +5.5 | 6.63 | ∞ | +19.8 | `c6edb8d6` |
| 123 | 10 | 90.0% | +102.9 | +93.4 | +9.5 | 8.38 | 720.85 | -5.3 | `d38fd691` |

### DELTA vs baseline — PriceChannelTrade `default ({})` (mean)

```text
baseline сам на себя (эталон семьи) — дельты 0.
Δ Net: +0.0
Δ PF: 0 (оба ∞)
Δ DD: +0.0
Δ Expectancy: +0.0
Δ Trades: +0.0
Δ WR: +0.0
Δ Commission / Δ Slippage: 0 / 0 (издержки не моделируются)
```

**STATUS:** CANDIDATE

---

## EXP-000010 — PriceChannelTrade · `{"length_down":10,"length_up":10}`

- **Robot**: PriceChannelTrade (`price_channel`)
- **Entry TF**: synthetic 1-bar (детерминированная серия, 400 баров)
- **Filter TF / Execution TF**: none
- **Exit**: логика порта (см. robots.py); trail_stop если задан в params
- **Dataset**: synthetic_series(n=400) × seeds [42, 7, 123]
- **Universe**: synthetic (1 инструмент)
- **Commission**: 0 · **Slippage**: 0

### Результат (mean по 3 seedам)

```text
TRADES: 10.0
WIN RATE: 100.0%
NET: +170.7   (GROSS/realized +160.3, OPEN/unrealized +10.4)
PF: ∞
EXPECTANCY: +16.0
AVG WIN / AVG LOSS: n/a (Wave A не разделяет)
MAX DD: 3.7
COMMISSION: 0   SLIPPAGE: 0
AVG HOLD / MEDIAN HOLD: n/a (Wave A не трекает)
IN MARKET: 96.9%   BUY&HOLD: +6.8
```

### По seedам

| seed | trades | wr | net | realized | unrealized | max_dd | pf | buy&hold | equity_sha256 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 7 | 10 | 100.0% | +171.5 | +159.0 | +12.5 | 4.45 | ∞ | +5.8 | `7c873ae7` |
| 42 | 10 | 100.0% | +180.3 | +172.6 | +7.7 | 3.25 | ∞ | +19.8 | `d30cc736` |
| 123 | 10 | 100.0% | +160.4 | +149.2 | +11.2 | 3.40 | ∞ | -5.3 | `db769b43` |

### DELTA vs baseline — PriceChannelTrade `default ({})` (mean)

```text
Δ Net: +63.6
Δ PF: 0 (оба ∞)
Δ DD: -3.6
Δ Expectancy: +6.1
Δ Trades: +0.0
Δ WR: +6.7
Δ Commission / Δ Slippage: 0 / 0 (издержки не моделируются)
```

**STATUS:** CANDIDATE

---

## EXP-000011 — PriceChannelTrade · `{"length_down":34,"length_up":34}`

- **Robot**: PriceChannelTrade (`price_channel`)
- **Entry TF**: synthetic 1-bar (детерминированная серия, 400 баров)
- **Filter TF / Execution TF**: none
- **Exit**: логика порта (см. robots.py); trail_stop если задан в params
- **Dataset**: synthetic_series(n=400) × seeds [42, 7, 123]
- **Universe**: synthetic (1 инструмент)
- **Commission**: 0 · **Slippage**: 0

### Результат (mean по 3 seedам)

```text
TRADES: 10.0
WIN RATE: 53.3%
NET: +4.6   (GROSS/realized +1.5, OPEN/unrealized +3.1)
PF: 1.09
EXPECTANCY: +0.1
AVG WIN / AVG LOSS: n/a (Wave A не разделяет)
MAX DD: 13.7
COMMISSION: 0   SLIPPAGE: 0
AVG HOLD / MEDIAN HOLD: n/a (Wave A не трекает)
IN MARKET: 90.8%   BUY&HOLD: +6.8
```

### По seedам

| seed | trades | wr | net | realized | unrealized | max_dd | pf | buy&hold | equity_sha256 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 7 | 10 | 70.0% | +7.2 | +3.9 | +3.3 | 12.35 | 1.29 | +5.8 | `70e91641` |
| 42 | 10 | 50.0% | +0.3 | -2.2 | +2.5 | 15.01 | 0.81 | +19.8 | `73b92bbe` |
| 123 | 10 | 40.0% | +6.1 | +2.7 | +3.4 | 13.88 | 1.18 | -5.3 | `a6358536` |

### DELTA vs baseline — PriceChannelTrade `default ({})` (mean)

```text
Δ Net: -102.6
Δ PF: → 1.09 (base ∞)
Δ DD: +6.4
Δ Expectancy: -9.8
Δ Trades: +0.0
Δ WR: -40.0
Δ Commission / Δ Slippage: 0 / 0 (издержки не моделируются)
```

**STATUS:** WATCH

---

## EXP-000012 — PriceChannelTrade · `{"regime":"OnlyLong"}`

- **Robot**: PriceChannelTrade (`price_channel`)
- **Entry TF**: synthetic 1-bar (детерминированная серия, 400 баров)
- **Filter TF / Execution TF**: none
- **Exit**: логика порта (см. robots.py); trail_stop если задан в params
- **Dataset**: synthetic_series(n=400) × seeds [42, 7, 123]
- **Universe**: synthetic (1 инструмент)
- **Commission**: 0 · **Slippage**: 0

### Результат (mean по 3 seedам)

```text
TRADES: 5.0
WIN RATE: 86.7%
NET: +51.2   (GROSS/realized +43.3, OPEN/unrealized +7.9)
PF: ∞*
EXPECTANCY: +8.7
AVG WIN / AVG LOSS: n/a (Wave A не разделяет)
MAX DD: 6.9
COMMISSION: 0   SLIPPAGE: 0
AVG HOLD / MEDIAN HOLD: n/a (Wave A не трекает)
IN MARKET: 46.8%   BUY&HOLD: +6.8
```

### По seedам

| seed | trades | wr | net | realized | unrealized | max_dd | pf | buy&hold | equity_sha256 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 7 | 5 | 80.0% | +50.2 | +41.5 | +8.7 | 5.98 | 93.50 | +5.8 | `7e392a5b` |
| 42 | 5 | 100.0% | +58.5 | +53.0 | +5.5 | 6.32 | ∞ | +19.8 | `522b5938` |
| 123 | 5 | 80.0% | +44.9 | +35.5 | +9.5 | 8.38 | 274.21 | -5.3 | `d5323438` |

### DELTA vs baseline — PriceChannelTrade `default ({})` (mean)

```text
Δ Net: -55.9
Δ PF: 0 (оба ∞)
Δ DD: -0.4
Δ Expectancy: -1.3
Δ Trades: -5.0
Δ WR: -6.7
Δ Commission / Δ Slippage: 0 / 0 (издержки не моделируются)
```

**STATUS:** CANDIDATE

---

## EXP-000013 — RsiContrtrend · `default`

- **Robot**: RsiContrtrend (`rsi_contrtrend`)
- **Entry TF**: synthetic 1-bar (детерминированная серия, 400 баров)
- **Filter TF / Execution TF**: none
- **Exit**: логика порта (см. robots.py); trail_stop если задан в params
- **Dataset**: synthetic_series(n=400) × seeds [42, 7, 123]
- **Universe**: synthetic (1 инструмент)
- **Commission**: 0 · **Slippage**: 0

### Результат (mean по 3 seedам)

```text
TRADES: 0.0
WIN RATE: 0.0%
NET: +0.0   (GROSS/realized +0.0, OPEN/unrealized +0.0)
PF: 0.00
EXPECTANCY: +0.0
AVG WIN / AVG LOSS: n/a (Wave A не разделяет)
MAX DD: 0.0
COMMISSION: 0   SLIPPAGE: 0
AVG HOLD / MEDIAN HOLD: n/a (Wave A не трекает)
IN MARKET: 0.0%   BUY&HOLD: +6.8
```

### По seedам

| seed | trades | wr | net | realized | unrealized | max_dd | pf | buy&hold | equity_sha256 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 7 | 0 | 0.0% | +0.0 | +0.0 | +0.0 | 0.00 | 0.00 | +5.8 | `471f59ae` |
| 42 | 0 | 0.0% | +0.0 | +0.0 | +0.0 | 0.00 | 0.00 | +19.8 | `471f59ae` |
| 123 | 0 | 0.0% | +0.0 | +0.0 | +0.0 | 0.00 | 0.00 | -5.3 | `471f59ae` |

### DELTA vs baseline — RsiContrtrend `default ({})` (mean)

```text
baseline сам на себя (эталон семьи) — дельты 0.
Δ Net: +0.0
Δ PF: +0.00
Δ DD: +0.0
Δ Expectancy: +0.0
Δ Trades: +0.0
Δ WR: +0.0
Δ Commission / Δ Slippage: 0 / 0 (издержки не моделируются)
```

**STATUS:** REJECT (нет входов)

---

## EXP-000014 — RsiContrtrend · `{"downline":30.0,"upline":70.0}`

- **Robot**: RsiContrtrend (`rsi_contrtrend`)
- **Entry TF**: synthetic 1-bar (детерминированная серия, 400 баров)
- **Filter TF / Execution TF**: none
- **Exit**: логика порта (см. robots.py); trail_stop если задан в params
- **Dataset**: synthetic_series(n=400) × seeds [42, 7, 123]
- **Universe**: synthetic (1 инструмент)
- **Commission**: 0 · **Slippage**: 0

### Результат (mean по 3 seedам)

```text
TRADES: 0.0
WIN RATE: 0.0%
NET: +0.0   (GROSS/realized +0.0, OPEN/unrealized +0.0)
PF: 0.00
EXPECTANCY: +0.0
AVG WIN / AVG LOSS: n/a (Wave A не разделяет)
MAX DD: 0.0
COMMISSION: 0   SLIPPAGE: 0
AVG HOLD / MEDIAN HOLD: n/a (Wave A не трекает)
IN MARKET: 0.0%   BUY&HOLD: +6.8
```

### По seedам

| seed | trades | wr | net | realized | unrealized | max_dd | pf | buy&hold | equity_sha256 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 7 | 0 | 0.0% | +0.0 | +0.0 | +0.0 | 0.00 | 0.00 | +5.8 | `471f59ae` |
| 42 | 0 | 0.0% | +0.0 | +0.0 | +0.0 | 0.00 | 0.00 | +19.8 | `471f59ae` |
| 123 | 0 | 0.0% | +0.0 | +0.0 | +0.0 | 0.00 | 0.00 | -5.3 | `471f59ae` |

### DELTA vs baseline — RsiContrtrend `default ({})` (mean)

```text
Δ Net: +0.0
Δ PF: +0.00
Δ DD: +0.0
Δ Expectancy: +0.0
Δ Trades: +0.0
Δ WR: +0.0
Δ Commission / Δ Slippage: 0 / 0 (издержки не моделируются)
```

**STATUS:** REJECT (нет входов)

---

## EXP-000015 — RsiContrtrend · `{"regime":"OnlyLong"}`

- **Robot**: RsiContrtrend (`rsi_contrtrend`)
- **Entry TF**: synthetic 1-bar (детерминированная серия, 400 баров)
- **Filter TF / Execution TF**: none
- **Exit**: логика порта (см. robots.py); trail_stop если задан в params
- **Dataset**: synthetic_series(n=400) × seeds [42, 7, 123]
- **Universe**: synthetic (1 инструмент)
- **Commission**: 0 · **Slippage**: 0

### Результат (mean по 3 seedам)

```text
TRADES: 0.0
WIN RATE: 0.0%
NET: +0.0   (GROSS/realized +0.0, OPEN/unrealized +0.0)
PF: 0.00
EXPECTANCY: +0.0
AVG WIN / AVG LOSS: n/a (Wave A не разделяет)
MAX DD: 0.0
COMMISSION: 0   SLIPPAGE: 0
AVG HOLD / MEDIAN HOLD: n/a (Wave A не трекает)
IN MARKET: 0.0%   BUY&HOLD: +6.8
```

### По seedам

| seed | trades | wr | net | realized | unrealized | max_dd | pf | buy&hold | equity_sha256 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 7 | 0 | 0.0% | +0.0 | +0.0 | +0.0 | 0.00 | 0.00 | +5.8 | `471f59ae` |
| 42 | 0 | 0.0% | +0.0 | +0.0 | +0.0 | 0.00 | 0.00 | +19.8 | `471f59ae` |
| 123 | 0 | 0.0% | +0.0 | +0.0 | +0.0 | 0.00 | 0.00 | -5.3 | `471f59ae` |

### DELTA vs baseline — RsiContrtrend `default ({})` (mean)

```text
Δ Net: +0.0
Δ PF: +0.00
Δ DD: +0.0
Δ Expectancy: +0.0
Δ Trades: +0.0
Δ WR: +0.0
Δ Commission / Δ Slippage: 0 / 0 (издержки не моделируются)
```

**STATUS:** REJECT (нет входов)

---

## EXP-000016 — RsiContrtrend · `{"rsi_length":14}`

- **Robot**: RsiContrtrend (`rsi_contrtrend`)
- **Entry TF**: synthetic 1-bar (детерминированная серия, 400 баров)
- **Filter TF / Execution TF**: none
- **Exit**: логика порта (см. robots.py); trail_stop если задан в params
- **Dataset**: synthetic_series(n=400) × seeds [42, 7, 123]
- **Universe**: synthetic (1 инструмент)
- **Commission**: 0 · **Slippage**: 0

### Результат (mean по 3 seedам)

```text
TRADES: 0.0
WIN RATE: 0.0%
NET: +0.0   (GROSS/realized +0.0, OPEN/unrealized +0.0)
PF: 0.00
EXPECTANCY: +0.0
AVG WIN / AVG LOSS: n/a (Wave A не разделяет)
MAX DD: 0.0
COMMISSION: 0   SLIPPAGE: 0
AVG HOLD / MEDIAN HOLD: n/a (Wave A не трекает)
IN MARKET: 0.0%   BUY&HOLD: +6.8
```

### По seedам

| seed | trades | wr | net | realized | unrealized | max_dd | pf | buy&hold | equity_sha256 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 7 | 0 | 0.0% | +0.0 | +0.0 | +0.0 | 0.00 | 0.00 | +5.8 | `471f59ae` |
| 42 | 0 | 0.0% | +0.0 | +0.0 | +0.0 | 0.00 | 0.00 | +19.8 | `471f59ae` |
| 123 | 0 | 0.0% | +0.0 | +0.0 | +0.0 | 0.00 | 0.00 | -5.3 | `471f59ae` |

### DELTA vs baseline — RsiContrtrend `default ({})` (mean)

```text
Δ Net: +0.0
Δ PF: +0.00
Δ DD: +0.0
Δ Expectancy: +0.0
Δ Trades: +0.0
Δ WR: +0.0
Δ Commission / Δ Slippage: 0 / 0 (издержки не моделируются)
```

**STATUS:** REJECT (нет входов)

---

## EXP-000017 — RsiTrade · `default`

- **Robot**: RsiTrade (`rsi_trade`)
- **Entry TF**: synthetic 1-bar (детерминированная серия, 400 баров)
- **Filter TF / Execution TF**: none
- **Exit**: логика порта (см. robots.py); trail_stop если задан в params
- **Dataset**: synthetic_series(n=400) × seeds [42, 7, 123]
- **Universe**: synthetic (1 инструмент)
- **Commission**: 0 · **Slippage**: 0

### Результат (mean по 3 seedам)

```text
TRADES: 9.0
WIN RATE: 100.0%
NET: +119.7   (GROSS/realized +111.5, OPEN/unrealized +8.2)
PF: ∞
EXPECTANCY: +12.4
AVG WIN / AVG LOSS: n/a (Wave A не разделяет)
MAX DD: 5.8
COMMISSION: 0   SLIPPAGE: 0
AVG HOLD / MEDIAN HOLD: n/a (Wave A не трекает)
IN MARKET: 88.1%   BUY&HOLD: +6.8
```

### По seedам

| seed | trades | wr | net | realized | unrealized | max_dd | pf | buy&hold | equity_sha256 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 7 | 9 | 100.0% | +123.8 | +113.5 | +10.3 | 6.32 | ∞ | +5.8 | `5bb88eee` |
| 42 | 9 | 100.0% | +115.1 | +108.7 | +6.5 | 6.63 | ∞ | +19.8 | `da3a97ba` |
| 123 | 9 | 100.0% | +120.2 | +112.3 | +7.9 | 4.36 | ∞ | -5.3 | `d7760f7c` |

### DELTA vs baseline — RsiTrade `default ({})` (mean)

```text
baseline сам на себя (эталон семьи) — дельты 0.
Δ Net: +0.0
Δ PF: 0 (оба ∞)
Δ DD: +0.0
Δ Expectancy: +0.0
Δ Trades: +0.0
Δ WR: +0.0
Δ Commission / Δ Slippage: 0 / 0 (издержки не моделируются)
```

**STATUS:** CANDIDATE

---

## EXP-000018 — RsiTrade · `{"downline":30.0,"upline":70.0}`

- **Robot**: RsiTrade (`rsi_trade`)
- **Entry TF**: synthetic 1-bar (детерминированная серия, 400 баров)
- **Filter TF / Execution TF**: none
- **Exit**: логика порта (см. robots.py); trail_stop если задан в params
- **Dataset**: synthetic_series(n=400) × seeds [42, 7, 123]
- **Universe**: synthetic (1 инструмент)
- **Commission**: 0 · **Slippage**: 0

### Результат (mean по 3 seedам)

```text
TRADES: 9.0
WIN RATE: 100.0%
NET: +131.9   (GROSS/realized +122.8, OPEN/unrealized +9.1)
PF: ∞
EXPECTANCY: +13.6
AVG WIN / AVG LOSS: n/a (Wave A не разделяет)
MAX DD: 5.9
COMMISSION: 0   SLIPPAGE: 0
AVG HOLD / MEDIAN HOLD: n/a (Wave A не трекает)
IN MARKET: 88.3%   BUY&HOLD: +6.8
```

### По seedам

| seed | trades | wr | net | realized | unrealized | max_dd | pf | buy&hold | equity_sha256 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 7 | 9 | 100.0% | +138.5 | +127.1 | +11.5 | 5.45 | ∞ | +5.8 | `999e263d` |
| 42 | 9 | 100.0% | +137.3 | +130.7 | +6.6 | 5.06 | ∞ | +19.8 | `a64ec539` |
| 123 | 9 | 100.0% | +119.9 | +110.6 | +9.3 | 7.28 | ∞ | -5.3 | `a5e3a2e3` |

### DELTA vs baseline — RsiTrade `default ({})` (mean)

```text
Δ Net: +12.2
Δ PF: 0 (оба ∞)
Δ DD: +0.2
Δ Expectancy: +1.3
Δ Trades: +0.0
Δ WR: +0.0
Δ Commission / Δ Slippage: 0 / 0 (издержки не моделируются)
```

**STATUS:** CANDIDATE

---

## EXP-000019 — RsiTrade · `{"downline":40.0,"upline":60.0}`

- **Robot**: RsiTrade (`rsi_trade`)
- **Entry TF**: synthetic 1-bar (детерминированная серия, 400 баров)
- **Filter TF / Execution TF**: none
- **Exit**: логика порта (см. robots.py); trail_stop если задан в params
- **Dataset**: synthetic_series(n=400) × seeds [42, 7, 123]
- **Universe**: synthetic (1 инструмент)
- **Commission**: 0 · **Slippage**: 0

### Результат (mean по 3 seedам)

```text
TRADES: 9.0
WIN RATE: 100.0%
NET: +97.4   (GROSS/realized +90.5, OPEN/unrealized +6.9)
PF: ∞
EXPECTANCY: +10.1
AVG WIN / AVG LOSS: n/a (Wave A не разделяет)
MAX DD: 7.3
COMMISSION: 0   SLIPPAGE: 0
AVG HOLD / MEDIAN HOLD: n/a (Wave A не трекает)
IN MARKET: 87.8%   BUY&HOLD: +6.8
```

### По seedам

| seed | trades | wr | net | realized | unrealized | max_dd | pf | buy&hold | equity_sha256 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 7 | 9 | 100.0% | +103.1 | +94.4 | +8.7 | 7.53 | ∞ | +5.8 | `572d1d4a` |
| 42 | 9 | 100.0% | +94.3 | +88.8 | +5.5 | 8.21 | ∞ | +19.8 | `939b73d8` |
| 123 | 9 | 100.0% | +94.9 | +88.2 | +6.6 | 6.02 | ∞ | -5.3 | `0a9f0374` |

### DELTA vs baseline — RsiTrade `default ({})` (mean)

```text
Δ Net: -22.3
Δ PF: 0 (оба ∞)
Δ DD: +1.5
Δ Expectancy: -2.3
Δ Trades: +0.0
Δ WR: +0.0
Δ Commission / Δ Slippage: 0 / 0 (издержки не моделируются)
```

**STATUS:** CANDIDATE

---

## EXP-000020 — RsiTrade · `{"rsi_length":14}`

- **Robot**: RsiTrade (`rsi_trade`)
- **Entry TF**: synthetic 1-bar (детерминированная серия, 400 баров)
- **Filter TF / Execution TF**: none
- **Exit**: логика порта (см. robots.py); trail_stop если задан в params
- **Dataset**: synthetic_series(n=400) × seeds [42, 7, 123]
- **Universe**: synthetic (1 инструмент)
- **Commission**: 0 · **Slippage**: 0

### Результат (mean по 3 seedам)

```text
TRADES: 9.0
WIN RATE: 100.0%
NET: +134.9   (GROSS/realized +125.1, OPEN/unrealized +9.8)
PF: ∞
EXPECTANCY: +13.9
AVG WIN / AVG LOSS: n/a (Wave A не разделяет)
MAX DD: 9.0
COMMISSION: 0   SLIPPAGE: 0
AVG HOLD / MEDIAN HOLD: n/a (Wave A не трекает)
IN MARKET: 88.7%   BUY&HOLD: +6.8
```

### По seedам

| seed | trades | wr | net | realized | unrealized | max_dd | pf | buy&hold | equity_sha256 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 7 | 9 | 100.0% | +138.4 | +126.9 | +11.5 | 7.97 | ∞ | +5.8 | `a649670e` |
| 42 | 9 | 100.0% | +150.5 | +142.8 | +7.7 | 4.17 | ∞ | +19.8 | `983ae26d` |
| 123 | 9 | 100.0% | +115.7 | +105.4 | +10.2 | 14.88 | ∞ | -5.3 | `98a8acb5` |

### DELTA vs baseline — RsiTrade `default ({})` (mean)

```text
Δ Net: +15.1
Δ PF: 0 (оба ∞)
Δ DD: +3.2
Δ Expectancy: +1.5
Δ Trades: +0.0
Δ WR: +0.0
Δ Commission / Δ Slippage: 0 / 0 (издержки не моделируются)
```

**STATUS:** CANDIDATE

---

## EXP-000021 — SmaStoch · `{"regime":"OnlyShort","step":1.0}`

- **Robot**: SmaStoch (`sma_stoch`)
- **Entry TF**: synthetic 1-bar (детерминированная серия, 400 баров)
- **Filter TF / Execution TF**: none
- **Exit**: логика порта (см. robots.py); trail_stop если задан в params
- **Dataset**: synthetic_series(n=400) × seeds [42, 7, 123]
- **Universe**: synthetic (1 инструмент)
- **Commission**: 0 · **Slippage**: 0

### Результат (mean по 3 seedам)

```text
TRADES: 0.0
WIN RATE: 0.0%
NET: +0.0   (GROSS/realized +0.0, OPEN/unrealized +0.0)
PF: 0.00
EXPECTANCY: +0.0
AVG WIN / AVG LOSS: n/a (Wave A не разделяет)
MAX DD: 0.0
COMMISSION: 0   SLIPPAGE: 0
AVG HOLD / MEDIAN HOLD: n/a (Wave A не трекает)
IN MARKET: 0.0%   BUY&HOLD: +6.8
```

### По seedам

| seed | trades | wr | net | realized | unrealized | max_dd | pf | buy&hold | equity_sha256 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 7 | 0 | 0.0% | +0.0 | +0.0 | +0.0 | 0.00 | 0.00 | +5.8 | `471f59ae` |
| 42 | 0 | 0.0% | +0.0 | +0.0 | +0.0 | 0.00 | 0.00 | +19.8 | `471f59ae` |
| 123 | 0 | 0.0% | +0.0 | +0.0 | +0.0 | 0.00 | 0.00 | -5.3 | `471f59ae` |

### DELTA vs baseline — SmaStoch `{"regime":"OnlyShort","step":1.0}` (mean)

```text
baseline сам на себя (эталон семьи) — дельты 0.
Δ Net: +0.0
Δ PF: +0.00
Δ DD: +0.0
Δ Expectancy: +0.0
Δ Trades: +0.0
Δ WR: +0.0
Δ Commission / Δ Slippage: 0 / 0 (издержки не моделируются)
```

**STATUS:** REJECT (нет входов)

---

## EXP-000022 — SmaStoch · `{"step":0.5}`

- **Robot**: SmaStoch (`sma_stoch`)
- **Entry TF**: synthetic 1-bar (детерминированная серия, 400 баров)
- **Filter TF / Execution TF**: none
- **Exit**: логика порта (см. robots.py); trail_stop если задан в params
- **Dataset**: synthetic_series(n=400) × seeds [42, 7, 123]
- **Universe**: synthetic (1 инструмент)
- **Commission**: 0 · **Slippage**: 0

### Результат (mean по 3 seedам)

```text
TRADES: 1.0
WIN RATE: 33.3%
NET: +4.1   (GROSS/realized +4.1, OPEN/unrealized +0.0)
PF: ∞*
EXPECTANCY: +4.5
AVG WIN / AVG LOSS: n/a (Wave A не разделяет)
MAX DD: 1.8
COMMISSION: 0   SLIPPAGE: 0
AVG HOLD / MEDIAN HOLD: n/a (Wave A не трекает)
IN MARKET: 3.8%   BUY&HOLD: +6.8
```

### По seedам

| seed | trades | wr | net | realized | unrealized | max_dd | pf | buy&hold | equity_sha256 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 7 | 2 | 0.0% | -2.6 | -2.6 | +0.0 | 2.63 | 0.00 | +5.8 | `a9f750b1` |
| 42 | 0 | 0.0% | +0.0 | +0.0 | +0.0 | 0.00 | 0.00 | +19.8 | `471f59ae` |
| 123 | 1 | 100.0% | +14.9 | +14.9 | +0.0 | 2.66 | ∞ | -5.3 | `59b3a883` |

### DELTA vs baseline — SmaStoch `{"regime":"OnlyShort","step":1.0}` (mean)

```text
Δ Net: +4.1
Δ PF: →∞ (base 0.00)
Δ DD: +1.8
Δ Expectancy: +4.5
Δ Trades: +1.0
Δ WR: +33.3
Δ Commission / Δ Slippage: 0 / 0 (издержки не моделируются)
```

**STATUS:** WATCH

---

## EXP-000023 — SmaStoch · `{"step":1.0}`

- **Robot**: SmaStoch (`sma_stoch`)
- **Entry TF**: synthetic 1-bar (детерминированная серия, 400 баров)
- **Filter TF / Execution TF**: none
- **Exit**: логика порта (см. robots.py); trail_stop если задан в params
- **Dataset**: synthetic_series(n=400) × seeds [42, 7, 123]
- **Universe**: synthetic (1 инструмент)
- **Commission**: 0 · **Slippage**: 0

### Результат (mean по 3 seedам)

```text
TRADES: 0.3
WIN RATE: 0.0%
NET: -0.9   (GROSS/realized -0.9, OPEN/unrealized +0.0)
PF: 0.00
EXPECTANCY: -0.9
AVG WIN / AVG LOSS: n/a (Wave A не разделяет)
MAX DD: 0.9
COMMISSION: 0   SLIPPAGE: 0
AVG HOLD / MEDIAN HOLD: n/a (Wave A не трекает)
IN MARKET: 0.6%   BUY&HOLD: +6.8
```

### По seedам

| seed | trades | wr | net | realized | unrealized | max_dd | pf | buy&hold | equity_sha256 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 7 | 1 | 0.0% | -2.6 | -2.6 | +0.0 | 2.63 | 0.00 | +5.8 | `1fa0ff75` |
| 42 | 0 | 0.0% | +0.0 | +0.0 | +0.0 | 0.00 | 0.00 | +19.8 | `471f59ae` |
| 123 | 0 | 0.0% | +0.0 | +0.0 | +0.0 | 0.00 | 0.00 | -5.3 | `471f59ae` |

### DELTA vs baseline — SmaStoch `{"regime":"OnlyShort","step":1.0}` (mean)

```text
Δ Net: -0.9
Δ PF: +0.00
Δ DD: +0.9
Δ Expectancy: -0.9
Δ Trades: +0.3
Δ WR: +0.0
Δ Commission / Δ Slippage: 0 / 0 (издержки не моделируются)
```

**STATUS:** WATCH

---

## EXP-000024 — SmaStoch · `{"step":2.0}`

- **Robot**: SmaStoch (`sma_stoch`)
- **Entry TF**: synthetic 1-bar (детерминированная серия, 400 баров)
- **Filter TF / Execution TF**: none
- **Exit**: логика порта (см. robots.py); trail_stop если задан в params
- **Dataset**: synthetic_series(n=400) × seeds [42, 7, 123]
- **Universe**: synthetic (1 инструмент)
- **Commission**: 0 · **Slippage**: 0

### Результат (mean по 3 seedам)

```text
TRADES: 0.0
WIN RATE: 0.0%
NET: +0.0   (GROSS/realized +0.0, OPEN/unrealized +0.0)
PF: 0.00
EXPECTANCY: +0.0
AVG WIN / AVG LOSS: n/a (Wave A не разделяет)
MAX DD: 0.0
COMMISSION: 0   SLIPPAGE: 0
AVG HOLD / MEDIAN HOLD: n/a (Wave A не трекает)
IN MARKET: 0.0%   BUY&HOLD: +6.8
```

### По seedам

| seed | trades | wr | net | realized | unrealized | max_dd | pf | buy&hold | equity_sha256 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 7 | 0 | 0.0% | +0.0 | +0.0 | +0.0 | 0.00 | 0.00 | +5.8 | `471f59ae` |
| 42 | 0 | 0.0% | +0.0 | +0.0 | +0.0 | 0.00 | 0.00 | +19.8 | `471f59ae` |
| 123 | 0 | 0.0% | +0.0 | +0.0 | +0.0 | 0.00 | 0.00 | -5.3 | `471f59ae` |

### DELTA vs baseline — SmaStoch `{"regime":"OnlyShort","step":1.0}` (mean)

```text
Δ Net: +0.0
Δ PF: +0.00
Δ DD: +0.0
Δ Expectancy: +0.0
Δ Trades: +0.0
Δ WR: +0.0
Δ Commission / Δ Slippage: 0 / 0 (издержки не моделируются)
```

**STATUS:** REJECT (нет входов)

---

## Как добавляются следующие роботы

1. Порт класса → `backend/app/engine/ose/robots.py`.  
2. Юнит-тесты → `backend/tests/test_ose_robots.py`.  
3. Сетка конфигов → `backend/scripts/bt_ose_sweep.py`.  
4. Прогон: `python3 scripts/bt_ose_sweep.py --out reports/bt_ose_sweep_<host>.json` → карточки EXP дописываются сюда.  
5. Межмашинная сверка бит-в-бит, как Wave A (эталон 72/72).