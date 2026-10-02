# TRAILING_STOPS — все трейлинг-механизмы кодовой базы в одном месте

Дата: 2026-10-02. Сводный документ: где живёт каждый трейл, чем отличается,
какие инварианты обязательны. Если меняешь трейлинг — проверь, что не сломал
параллельные реализации (у нас их 4 семейства) и обнови этот файл.

---

## 0. Карта реализаций

| # | Где | Кто использует | Активация | Дистанция | Исполнение |
|---|-----|----------------|-----------|-----------|------------|
| 1 | `app/engine/exits.py::AtrStopPolicy` | **бот (runtime)**, E5/бэктест | комиссионная (`trail_activation_comm_mult`) или ATR (`trail_activation_r`) | `trail_distance_r` × (ATR в комиссионном режиме / risk в ATR-режиме) + дин. сжатие | `intrabar_exit` (close_based после активации) |
| 2 | `app/engine/exits.py::AtrTrailingPolicy` | legacy-планировщик входа | move ≥ `activation_atr`×ATR | `trail_distance_atr`×ATR от окна period баров | снаружи |
| 3 | `app/engine/regime_strategies.py::ExitManager` | regime-стратегии (движок) | включена сразу (`enable_trailing`) | `default_atr_mult_trail`×ATR от running max/min | `ExitDecision(TRAILING_STOP)` по бару |
| 4 | `app/engine/ose/robots.py` (`TesterTab.close_at_trailing_stop`) | OSE-порт (13 роботов) | сразу при входе / каждом баре | заданная цена-кандидат, ratchet | внутрибарно по касанию, `trail_close` |

Общий принцип **всех**: ratchet — стоп двигается только в сторону прибыли.
Исключений нет ни в одной реализации.

---

## 1. Боевой трейл бота: `AtrStopPolicy` v1.2.0 (`app/engine/exits.py`)

Конфиг-поля (`BotConfig`, runtime.py ~175):

```python
trail_activation_comm_mult: float | None = None  # None = трейлинг ВЫКЛЮЧЕН
trail_info_activation_comm_mult: float | None    # отдельный инфо-трейл (метрика)
trail_distance_atr: float = 2.5   # дистанция = N × ЧИСТЫЙ ATR
trail_compress_r: float = 1.0     # сжатие дистанции по прибыли в R
trail_min_factor: float = 0.6     # пол сжатия (2.5 → 1.5×ATR)
trail_min_atr: float = 1.5        # минимальная дистанция в ATR
trail_vol_boost: float = 0.3      # объём: высокий → шире, низкий → теснее
```

Пайплайн одного пересчёта (`update_stop`):

1. **Активация.** Комиссионный режим: `PnL(close) >= trail_activation_comm_mult × комиссия_входа`
   (`trailing_activated`; без qty/комиссии не активируется никогда — защита аналитики).
   ATR-режим (бэктест): `move(пик) >= trail_activation_r × risk`.
2. **База отсчёта — пик с момента входа** (`peak_price`: running max high LONG /
   min low SHORT — то же поле, что трекает пик-PnL бота `_peak_pnl`).
   Fallback без пика — окно `period` баров, но окно может содержать бары ДО
   входа → стоп выше входа (баг OZON 10.09.2026, хай 2724 → стоп 2650 при цене 2623).
   **Передавать `peak_price` из рантайма обязательно.**
3. **Дистанция** `dist = trail_distance_r × unit`, где unit = чистый ATR (бот) или
   risk (бэктест). Затем динамическая часть:
   - сжатие: `factor = max(trail_min_factor, 1 − trail_compress_r × max(0, R))`;
   - объём: `adj = clamp(sqrt(vol_ratio), 1−boost, 1+boost)`;
   - пол: `dist = max(dist, trail_min_atr × unit)`.
4. **Кандидат и ratchet:** LONG `max(current, highest − dist)`, SHORT `min(current, lowest + dist)`.

ATR-кэш (то же семейство, что `AtrTrailingPolicy`): валидность = первый ts **И**
последний закэшированный бар (ts + close). Проверка только первого ts и длины
молча подсовывала чужой ATR другой серии (аудит 2026-09-18).

### Инфо-трейл

`trail_info_activation_comm_mult` — отдельный трекер (`self._trail_info`,
runtime ~877): не двигает стоп, а записывает метрику «куда бы встал трейл»
(`trail_stop`, `trail_dist_atr`, `act_track`) в `meta["trail_info"]` при закрытии.
Используется для анализа недополученной прибыли после закрытия.

---

## 2. Исполнение в боте: `intrabar_exit` + `close_based`

`app/engine/exits.py::intrabar_exit(bar, state, stop, tp, close_based, wick_tol)`:

- `close_based=False` (движок/бэктест): классика — касание `bar.low`/`bar.high`
  закрывает позицию.
- `close_based=True` (бот, **после активации трейлинга**): стоп срабатывает только
  если бар **закрылся** за уровнем или открылся за ним (гэп). Хвост, коснувшийся
  стопа и вернувшийся, не выбивает — защита от ложных пробоев.
- `wick_tol`: «прощение хвоста» только для НАЧАЛЬНОГО стопа (не трейлинга):
  прокол не глубже допуска + закрытие обратно — позиция живёт. `wick_tol=0` —
  байт-в-байт прежнее поведение.

Приоритет на одном баре: `STOP_LOSS_FIRST` (`SAME_BAR_CONFLICT_RULE`).
Восстановление после рестарта: runtime пересоздаёт `AtrStopPolicy` и восстанавливает
`_trail_active` / `_trail_stop` из run-state (строки ~3761–3790).

---

## 3. Движковые трейлы

### 3.1 `AtrTrailingPolicy` (exits.py, legacy)

Простой: активация `move(окна) >= activation_atr×ATR`, кандидат
`peak − trail_distance_atr×ATR`, ratchet через `max/min`. Используется как
планировщик входа (`plan_entry`) + `update_stop` в старых пайплайнах.
Не смешивать с боевым `AtrStopPolicy`!

### 3.2 `ExitManager` / regime-стратегии (`app/engine/regime_strategies.py`)

`TRAILING_STOP` как `ExitReason`. Логика (`~277`):

- активация: включена сразу при `enable_trailing` и сигналах trend/breakout;
- база: `position.highest_price` / `lowest_price` (running экстремум позиции);
- дистанция: `default_atr_mult_trail` (2.5) × ATR;
- ratchet: новый кандидат принимается только если лучше текущего;
- выход: касание уровня следующим баром → `ExitDecision(True, TRAILING_STOP, stop)`.

Эталонный фрагмент — Приложение B (байт-в-байт из кода).

---

## 4. OSE-порт: `CloseAtTrailingStop` (`app/engine/ose/robots.py`)

Канон из OsEngine (`PORT_NOTES_EXITS.md`): **трейлинг — не сущность, а
перезарядка стоп-слота позиции новой ценой каждый бар.** Стоп и тейк — OCO-пара
слотов на позиции.

Механика `TesterTab.close_at_trailing_stop(position, activation_price, order_price)`
(~306):

- при `activation_price` → стоп-заявка (тестерная семантика: филл по цене активации);
- **ratchet на слоте**: стоп лонга не опускается, шорта не поднимается;
- `position.stop_is_trail = True` → при срабатывании `exit_reason = "trail_close"`
  (иначе `stop_close`). При обычном `close_at_stop` флаг сбрасывается (~275, ~300).

Эталонный фрагмент — Приложение A (байт-в-байт из кода).

Роботы с трейлом (13 всего, перезарядка каждым баром):

| Робот | Активация | Дистанция |
|---|---|---|
| `EnvelopTrend` (~527) | сразу после входа, от канала | `TrailStop%` от `up`/`down` канала |
| `MacdTrail` (~971) | каждый бар | `close ∓ trail_stop%` |
| `BollingerTrailing` (~1070) | каждый бар | нижняя/верхняя граница BB |
| `SmaTrendSample` (~1114) | каждый бар | стоп с отступом slip |
| `PriceChannelVolatility` (~1179) | каждый бар | граница канала |

Поведенческие контракты — тесты `tests/test_ose_robots.py`:
`test_exit_slot_trailing_migrates_stop_each_bar` (перезарядка ползёт вверх),
`test_exit_slot_trailing_ratchet_keeps_better_stop` (нельзя назад).

---

## 5. Инварианты (не нарушать ни в одной реализации)

1. **Ratchet**: стоп только в сторону прибыли. Общий код и OSE-слот соблюдают.
2. **Трейл активируется только на СЛЕДУЮЩЕМ баре после пересчёта** (ENG-002,
   `test_trailing_update_becomes_active_next_bar_only`): подтянутый стоп не может
   выбить позицию на баре его пересчёта.
3. **База = экстремум с момента входа**, не окно, содержащее пре-входовые бары
   (баг OZON). В боте передаём `peak_price`.
4. **Комиссионная активация требует реальных qty и комиссии**; аналитические
   вызовы без них не активируют трейл молча — это фича, не баг.
5. **close_based после активации** в боте: трейл-стоп не снимается хвостом.
6. **ATR-кэш валиден только при совпадении первого ts И последнего бара**
   (ts + close).
7. **Стоп-первым** при конфликте стоп/тейк на одном баре.

---

## 6. Инструменты вокруг

- `scripts/optuna_trail_sim.py` — порт боевого трейла на 5m-бары для Optuna:
  активация комиссионная, ratchet, сжатие; `BOT_DEFAULT = {sl_mult 2.0, trail 2.5,
  act_mult 4.0}`. Держать в синхроне с `AtrStopPolicy` при изменении формул.
- `breakeven_stop` (exits.py) — BE-перенос с тем же ratchet-инвариантом;
  независим от трейла, но применяется до него по цепочке выходов.
- `app/engine/exits.py::intrabar_exit` — единственная точка интрабарного
  исполнения для движка и бота.
- Инфо-трейл: `trail_info_activation_comm_mult` → `meta["trail_info"]` в сделках.

## 7. Что менять при тюнинге параметров

| Хочу | Крути |
|---|---|
| позже/раньше включается | `trail_activation_comm_mult` (бот) / `trail_activation_r` (бэктест) |
| шире/уже следует | `trail_distance_atr` |
| прижимается при прибыли | `trail_compress_r` ↑, пол `trail_min_factor` |
| не даёт выбить шумом | `trail_min_atr` ↑, у бота уже включён close_based |
| реагирует на объём | `trail_vol_boost` |
| отключить совсем | `trail_activation_comm_mult: null` (None = выключено, в т.ч. в пресетах) |

---

## Приложение. Эталонные фрагменты (байт-в-байт из кода)

### A. OSE-слот: ratchet-гварды (`app/engine/ose/robots.py`, `close_at_trailing_stop`)

```python
def close_at_trailing_stop(self, position: Position, activation_price: float,
                           order_price: float) -> None:
    """CloseAtTrailingStop: трейлинг с ratchet — стоп лонга двигается
    только вверх, стоп шорта только вниз. Гварды оригинала: при
    RedLine > activation (Buy) или RedLine < activation (Sell) новый
    уровень не применяется, остаётся лучший из уже стоящих."""
    current = position.stop
    if current is not None:
        if position.side is Side.BUY and current[0] > activation_price:
            return
        if position.side is Side.SELL and current[0] < activation_price:
            return
    self.close_at_stop_market(position, activation_price, order_price)
    position.stop_is_trail = True
```

### B. Движковый трейл regime-стратегий (`app/engine/regime_strategies.py`, `ExitManager`)

```python
# 3) Trailing
if self.enable_trailing and position.trailing_stop is not None:
    atr = _atr_last(candles, 14)
    if atr is not None:
        if position.side == Side.BUY and position.highest_price is not None:
            nt = position.highest_price - atr * self.default_atr_mult_trail
            if nt > position.trailing_stop:
                position.trailing_stop = nt
            if low <= position.trailing_stop:
                return ExitDecision(True, ExitReason.TRAILING_STOP, position.trailing_stop)
        elif position.side == Side.SELL and position.lowest_price is not None:
            nt = position.lowest_price + atr * self.default_atr_mult_trail
            if position.trailing_stop is None or nt < position.trailing_stop:
                position.trailing_stop = nt
            if high >= position.trailing_stop:
                return ExitDecision(True, ExitReason.TRAILING_STOP, position.trailing_stop)
```
