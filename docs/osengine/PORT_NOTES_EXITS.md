# PORT_NOTES_EXITS — механика выходов (стоп/тейк/трейлинг) в OsEngine и план порта

Дата: 2026-09-28.
Источник: `~/OsEngine/project/OsEngine` — `OsTrader/Panels/Tab/BotTabSimple.cs`,
`Entity/Position.cs`, `Entity/PositionOpenerToStop.cs`.
Суть в одном абзаце: **стоп и тейк — это два слота на самой позиции (OCO-пара),
а «трейлинг» — не сущность, а повторная перезагрузка стопа новыми ценами каждый бар.**
Всё остальное — детали активации и исполнения.

---

## 1. Данные на позиции (Entity/Position.cs)

Позиция несёт две пары полей — «стоп» и «профит»:

| Стоп | Профит |
|---|---|
| `StopOrderRedLine` — цена активации | `ProfitOrderRedLine` |
| `StopOrderPrice` — цена исполнения | `ProfitOrderPrice` |
| `StopOrderIsActive` | `ProfitOrderIsActive` |
| `StopIsMarket` (market vs limit) | `ProfitIsMarket` |
| `StopIsIceberg` (+ count/milliseconds) | `ProfitIsIceberg` |

Плюс `SignalTypeStop` / `SignalTypeProfit` — строка-метка сигнала, при активации
копируется в `SignalTypeClose` (попадает в журнал/аналитику закрытия).

Важно: стоп и тейк — **поля позиции, а не отдельные ордера**. Максимум один
активный стоп и один тейк на позицию, связаны OCO (см. п.4).

## 2. Установка (BotTabSimple.cs, ~4620–5150)

- `CloseAtStopLimit(pos, activation, order)` / `CloseAtStopMarket(pos, activation)` — ~4630
- `CloseAtProfitLimit(...)` / `CloseAtProfitMarket(pos, activation)` — ~5018
- `CloseAtTrailingStopLimit/Market(pos, activation)` — ~4903/4941: **то же самое,
что обычный стоп**, только имя намекает роботу на намерение перезагружать.
  Внутри — запись `StopOrderRedLine/Price`, `StopIsMarket=true`, `IsActive=true`.
- Перегрузки с `signalType` — заполняют `SignalTypeStop`/`SignalTypeProfit`.

Семантика reload уже в установке: если пара уже активна с **теми же** ценами —
no-op (ранний return), иначе перезапись. В IsOptimizer/IsTester — упрощение:
`StopOrderPrice = priceActivate` (маркет), плюс в профите попытка clamp
«тейк не глубже рынка» (закомментировано в TryReloadProfit).

## 2a. Полная таксономия Close*-методов (инвентаризация 2026-09-28)

`public void Close*` в BotTabSimple.cs — ~40 методов (~4200–5420), но все
сводятся к четырём группам:

1. **Мгновенный выход по сигналу**: `CloseAtMarket` (~4370/4440),
   `CloseAtLimit` (~4452/4487/4500), `CloseAllAtMarket` (~4224/4252),
   `CloseAtFake` (~4276 — тестовая заглушка: снимает стоп/тейк-слоты и
   закрывает по заданной цене, минуя ордерную механику).
2. **Позиционный стоп**: `CloseAtStop` (limit), `CloseAtStopMarket`,
   `CloseAtStopCancel` (снять только стоп-слот, ~4652/4702) + iceberg- и
   OnServer-варианты (`CloseAtStopOnServer*` ~5286+ — реальный стоп-ордер на
   бирже через TryPlaceStopOrderOnServer, с fallback на локальный стоп).
3. **Позиционный тейк**: `CloseAtProfit`, `CloseAtProfitMarket` (~4995/5018)
   + iceberg.
4. **Трейлинг**: `CloseAtTrailingStop(Limit/Market)` (~4903/4941) — те же
   стоп-слоты, перезагружаемые роботом каждый бар.

Статистика вызовов по всему проекту OsEngine (включая UI и ручные режимы):
CloseAtMarket 297, CloseAtLimit 255, CloseAtTrailingStop 129,
CloseAtTrailingStopMarket 117, CloseAtStopMarket 111, CloseAtStop 100,
CloseAtProfitMarket 97, CloseAtProfit 92, CloseAllOrderToPosition 47,
остальное — единицы. То есть сигнальные закрытия доминируют по числу
вызовов (в т.ч. из UI), а стоп/тейк/трейлинг — по критичности: без них
позиция ничем не защищена.

Для бар-реплей порта значимы только: стоп/тейк-слоты с OCO и reload (п.7),
трейлинг как повторный reload, сигнальное закрытие (у нас уже есть
`close_at_limit`), `CloseAllAtMarket` тривиален. Iceberg, OnServer, Fake —
инфраструктура исполнения, в каркас не портируем.

## 3. Перезагрузка: TryReloadStop / TryReloadProfit (~6589 / ~6679)

```
if (позиция Done/OpeningFail) return;
if (уже активна && цены те же) return;      // защита от лишних перезаписей
объём = OpenVolume; if (0) return;
IsActive = false; -> новые RedLine/Price -> IsActive = true;
журнал/чарт/save
```

**Так и делается трейлинг**: робот на каждом баре/тике вызывает
`CloseAtTrailingStop` с новой ценой активации, TryReload переносит «красную
линию». Автотрейлинга внутри таба нет — двигает только сам робот.
Из UI то же делает `ManualPositionSupport.TryReloadStopAndProfit`.

## 4. Активация: CheckStop(pos, lastTrade) (~7013)

Вызывается откуда:
- **OnMarketDepth** (~7803/7813) — тестер/оптимизатор: `lastTrade` = лучшая цена
  нужной стороны стакана (Buy-позиции — по Bid, Sell — по Ask);
- **OnTrade** (~8304) — живой режим: по последней сделке.

Гейты: `StopOrderIsActive || ProfitOrderIsActive`, сервер подключён,
`OpenVolume != 0`. Условия (long — Buy-позиция):

| | активация |
|---|---|
| стоп long | `StopOrderRedLine >= lastTrade` |
| тейк long | `ProfitOrderRedLine <= lastTrade` |
| стоп short | `StopOrderRedLine <= lastTrade` |
| тейк short | `ProfitOrderRedLine >= lastTrade` |

При активации **любого** из пары обнуляются **оба** флага — это и есть OCO:
сработал стоп → тейк снят, сработал тейк → стоп снят. Сигнал
`SignalTypeStop/Profit` → `SignalTypeClose`, затем исполнение:
`CloseDeal(limit, StopOrderPrice/ProfitOrderPrice)` или market по
`StopIsMarket/ProfitIsMarket` (в OsTrader — iceberg-вариант), события
`PositionStopActivateEvent` / `PositionProfitActivateEvent`.

## 5. Стопы на ВХОД — отдельная сущность, не путать

`BuyAtStop/SellAtStop` — отложенные стоп-заявки на вход, лежат списком на табе
(у нас — `TesterTab.pending_stops`). Позиционные стопы — только про ВЫХОД.
Два разных механизма, в OsEngine никак не пересекаются.

## 6. Что у нас сейчас (app/engine/ose/robots.py) — где «шатко»

- Входы (`StopOrder` + `pending_stops`) — ок, соответствует п.5.
- Выход — один слот на весь таб: `trail: tuple[Position, activation, order_price] | None`:
  - тейк ставить **нечем** — profита в каркасе нет вообще;
  - нет OCO — стоп и тейк существовали бы независимо;
  - `close_at_trailing_stop` хранит статичную пару, сам «не трейлит» (это
    нормально — в OsEngine тоже), но нет reload-проверки «цены не изменились»
    и различия market/limit;
  - исполнение всегда по `order_price`, гэп открытия за активацией не учтён;
  - порядок в `process_intrabar` — выход до входов: это совпадает с духом OsEngine,
    оставляем.

## 7. План порта (бар-реплей)

1. **Position — два слота выхода:**
   ```python
   @dataclass
   class ExitOrder:
       activation_price: float          # StopOrderRedLine / ProfitOrderRedLine
       order_price: float               # StopOrderPrice / ProfitOrderPrice
       is_market: bool = True
       kind: str = "stop"               # "stop" | "profit"
       signal: str = ""                 # SignalTypeStop/Profit
   ```
   `position.stop: ExitOrder | None`, `position.profit: ExitOrder | None`.

2. **TesterTab API — маппинг 1:1:**
   - `close_at_stop(pos, activation, order=None, is_market=True, signal="")` → семантика TryReloadStop (no-op при тех же ценах, иначе перезапись);
   - `close_at_profit(pos, activation, order=None, is_market=True, signal="")` → TryReloadProfit;
   - `close_at_trailing_stop(pos, activation, signal="")` → алиас `close_at_stop(is_market=True)`; трейлинг = робот вызывает это каждый бар с новой ценой;
   - снятие пары при активации — внутри движка (см. п.4), наружу события не нужны.

3. **process_intrabar, порядок:** (1) позиционные стоп/тейк по диапазону бара,
   OCO — активация одного снимает другой; (2) входные стопы, только если нет
   открытых позиций (как сейчас).

4. **Активация по бару (аппроксимация CheckStop):** long — стоп при
   `low <= activation`, тейк при `high >= activation`; short — зеркально.
   Если в баре коснулись оба — считать сработавшим **стоп** (консервативно:
   диапазон не даёт порядка тиков).

5. **Исполнение:** limit — по `order_price`; market — по `activation_price`,
   но если бар **открылся за активацией** (гэп) — по `open` (хуже для нас).
   Это стандартная бар-реплей аппроксимация тикового «market по факту».

6. **Журнал:** в `fills` писать тег — `("stop_close", price)` /
   `("profit_close", price)` вместо общего `"close"` (аналог
   SignalTypeStop→SignalTypeClose). Слой голосов (`ose/strategy.py`) смотрит
   на открытие/закрытие — общий `close` оставить как fallback, голоса не меняются.

## 8. Тесты (добавить в tests/test_ose_robots.py)

- OCO: сработал стоп → тейк снят (и наоборот); после активации оба слота None.
- Reload: повторный `close_at_stop` с теми же ценами — no-op (fills не растут);
  с новыми — красная линия переехала (сценарий трейлинга на двух барах).
- Гэп: `open` хуже активации → исполнение по `open`.
- Приоритет: в баре, где касание стопа и стоп-заявка на вход — исполняется только выход.
- limit vs market цена исполнения; short — зеркальные условия.

## 9. Ссылки на код OsEngine

- `BotTabSimple.cs`: CloseAtStop* ~4620–4790, CloseAtTrailingStop* ~4903–4995,
  CloseAtProfit* ~5018–5150, TryReloadStop ~6589, TryReloadProfit ~6679,
  CheckStop ~7013, вызовы из OnMarketDepth ~7803/7813, из OnTrade ~8304.
- `Entity/Position.cs` — поля Stop*/Profit*.
- `Entity/PositionOpenerToStop.cs` — параметры стоп-заявки на вход (RedLine,
  Price, ActivateType, LifeTime) — к п.5.
- `docs/osengine/PORT_NOTES_ENGINE.md` — общий контекст каркаса.
