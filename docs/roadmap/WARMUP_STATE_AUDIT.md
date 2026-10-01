# WARMUP/STATE AUDIT (Этап A) — что греется, зачем, и что должно стать StateSnapshot

> Рамка (архитектор, 30.09): warmup — это **способ построения состояния**, а не способ запуска
> движка. REF-001 остаётся **cold reference**. State Snapshot делаем ТОЛЬКО после фиксации
> канонического market-data пути. Текущий warmup не трогаем — он остаётся fallback'ом, пока
> не доказана эквивалентность cold == warm.

## 1. Что реально греется сейчас (по коду)

| Слой | Механика | Сколько |
|---|---|---|
| Bot preload (одиночный движок) | `_load_one`: `ensure_candles(days=7)` + чтение **TF-строк из БД** `date_from=now−3d → date_to=now`, в буфер `deque(maxlen=MAX_BUFFER)`; `MAX_BUFFER=300` | ~3 торговых дня TF-баров (=300) |
| Bot preload (ensemble) | 10 дней 1m (`ENSEMBLE_BUFFER=14400`, ema_slow=50 + запас) | 10 дней |
| Engine (REF/харнесс) | грузит 1m с `dfrom−warmup_days`, канон-ресемпл, прогон `on_bar` по всей серии; сигналы подавляются первые `strategy.warmup_bars()` | 3 дня (REF-001) |
| Границы окна | `preset.py`: `replay_start = d0T04:00Z`; preload `date_to=_bot_now()` (включает бар ts=04:00 до его закрытия, ENG-015 replace); корзина **03:50** (1-минутное открытие утренней сессии) есть в серии движка (загрузка с 00:00), но не в live-окне replay | — |

Важно: 3 дня — это **размер буфера**, а не математическая необходимость. Минимальные окна
индикаторов сильно меньше (ниже).

## 2. Таблица: что греется → зачем → минимальное состояние → сериализуемо → где живёт

| Компонент | Зачем история | Минимальное состояние | Сериализуемо сейчас? | Где сейчас |
|---|---|---|---|---|
| Resampler | незакрытый бакет | `{bucket_ts, o,h,l,c,v}` per figi + конвенция (START) | да (мелкое) | память процесса (ReplayFeed/фид) |
| IndicatorHub: RSI | рекуррентное Wilder | `ag, al, len, n` (скаляры) | да | память hub (фаза commit уже пишет состояние) |
| IndicatorHub: EMA | рекуррентное | последнее значение + параметр | да | память hub |
| IndicatorHub: ATR/ADX | Wilder-сглаживание | `moving`/сглаженные DM, TR-кольцо | да | память hub |
| IndicatorHub: SMA/Bollinger | окно | rolling sum + ring последних L closes (или ring-only) | да | память hub |
| IndicatorHub: Stochastic | окна P1×2 + сглаживания | ring highs/lows P1 + средние tM1/tM2 | да | память hub |
| IndicatorHub: percentile (regime) | окно 200 | ring ATR% (200) | да | вне hub (regime) |
| Streak/rolling high-low (роботы) | окно | ring/extrema + счётчики | да | TesterTab (тяжелее всех) |
| RegimeDetector | EMA20/50 + дрейф 6 + консистентность 6 + ATR%-percentile 200 | 2–3 EMA + последние 6 баров + ring 200 | да | пересчёт по буферу на каждом старте |
| Signal/Strategy (канон) | состояния своих индикаторов | скаляры: `prev_hist`, `prev_close`, счётчики (+hub-стейт RSI) | да | память стратегии |
| Strategy (OSE-порты) | полный TesterTab | ордера/слоты/трейл-ratchet/индикаторные ряды | да, но **тяжёлый** — нужен отдельный контракт | память робота |
| ExitPolicy/позиция | стоп/пик/ratchet | уровень стопа/цели, пик, флаги активации | состояние позиции (отдельно) | память/БД (sandbox_trades) |
| TradeLedger | не нужен для расчёта | — | — | БД/память |

**Минимальные окна (ориентир):** RSI 22 бара; ATR 14; Stochastic ~P1+P2+P3; MACD 26+9→35;
Envelops 12; rsi_trade 25; OSE-роботы до ~52 (rsi_contrtrend SMA50+Rsi); **Regime ~200**
(ATR%-процентиль) + EMA50 — самое длинное. Т.е. 300 TF-баров буфера — с запасом на всё, кроме,
возможно, ensemble-пути (10 дней).

## 3. Этапы B–E (порядок работ, по рамке архитектора)

**B. State Contract** (эскиз):
```python
@dataclass(frozen=True)
class EngineStateSnapshot:
    instrument: str            # figi
    timeframe: str
    as_of: datetime            # состояние НЕПОСРЕДСТВЕННО ПЕРЕД первым входящим баром
    data_version: str          # хэш 1m-данных
    resampler_version: str
    indicator_version: str
    strategy_version: str
    indicator_state: dict      # per-indicator (RSI: ag/al; EMA: last; rings — компактно)
    strategy_state: dict       # light-скаляры канона; для OSE-портов — отдельный тяжёлый контракт
    fingerprint: str
```
Не `pickle(engine)`: явные снимки `MarketStateSnapshot / IndicatorStateSnapshot /
StrategyStateSnapshot`.

**C. Persistence:** таблица `engine_state_snapshots` с ключом
`(instrument, timeframe, strategy_id, strategy_version, as_of, state_version)`.

**D. Restore:** `engine.restore(snapshot)` → `on_bar(bar)` ведёт себя как после полного cold warmup.

**E. Reference test (ключевой):**
```
одни данные ──┬─→ COLD RUN (история→индикаторы→сигналы→ledger) ─┐
              └─→ SNAPSHOT RUN (restore→on_bar→ledger) ─────────┤
                                                                └─→ compare:
                                                                    indicator values, regime,
                                                                    signals, orders, fills,
                                                                    ledger, equity, fingerprint
```
Пока cold.fingerprint != warm.fingerprint — snapshot не внедряем; warmup остаётся fallback:
`snapshot exists+compatible → restore; иначе → cold warmup`.

## 4. Обязательный инвариант ДО снапшотов (канон market-data пути)

```
1m source → Canonical Resampler → TF bars → IndicatorHub → RegimeDetector → Strategy
```
- Инвариант: **DB-TF бары == Resampler TF бары** (тест; после фикса `candle_cache` → START).
- Пересобрать ТФ-таблицы всех фиг (сейчас SBER пересобран вручную; остальные — старая END-сетка).
- Research-скрипты (labeling/calibrate/trades_split) на `build_tf` → канонический Resampler.

## 5. Выводы Этапа A

1. 3 дня прогрева — это `MAX_BUFFER=300` TF-баров, не минимум; реальная математика — до ~200 баров
   (regime) у канона и до ~52 у OSE-портов.
2. Всё необходимое для продолжения расчёта — **компактное состояние по каждому индикатору**
   (скаляры + кольца), оно уже частично формализовано в IndicatorHub (commit-фаза).
3. Смешение швов (03:50/04:00, preload-граница, END→START) — это дефекты **границ окна и
   каноничности данных**, а не «прогрев плохой»; чинятся контрактом `as_of перед 04:00` +
   инвариантом БД-TF.
4. Дальше: (1) инвариант БД-TF == Resampler (+пересбор), (2) State Contract + таблица,
   (3) cold==warm reference test, (4) только затем — warm start в runtime/replay.
