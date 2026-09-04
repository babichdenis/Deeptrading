"""
Полный цикл-тест стримов в sandbox после открытия MOEX:
  1. StreamManager (positions+trades) стартует, snapshot ALRS SHORT 110
  2. Закрываем ALRS (BUY 11 лотов)
  3. Ждём: стрим должен показать ALRS закрыта (или позиция ушла)
  4. Открываем SBER LONG 1 лот
  5. Ждём: стрим показывает SBER LONG 1
  6. Закрываем SBER (SELL 1 лот)
  7. Ждём: стрим показывает SBER закрыта
Вывод: каждые 2с печатаем _positions из StreamManager и broker.get_position.
"""
import asyncio
import logging
import warnings
import sys
from uuid import uuid4

warnings.filterwarnings("ignore", category=DeprecationWarning)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
sys.path.insert(0, "/Users/Denis/Dev/Deeptrading/backend")

TOKEN = "t.Qhvl9v-tXNNrDLAw0AATld17wzqZ0E_CLJzkmp5AAoTZO92sJLdZxdVEGtpcOTrEZw1PfdXusqhRFhqONcU4Rw"
ACC = "413306e6-f634-4aef-a553-c84e764b298a"
ALRS_UID = "30817fea-20e6-4fee-ab1f-d20fc1a1bb72"
SBER_UID = "e6123145-9665-43e0-8413-cd61b8aa9b13"
ALRS_FIGI = "BBG004S68B31"
SBER_FIGI = "BBG004730N88"
SBER_LOT = 1

ST = {}

def log_state(tag):
    from app.bot.stream_manager import StreamManager
    sm = ST["sm"]
    print(f"\n=== {tag} ===", flush=True)
    print(f"  stream positions ({len(sm.get_positions())}):", flush=True)
    for p in sm.get_positions():
        print(f"    {p.figi} {p.side} {p.qty} entry={p.entry_price:.2f}", flush=True)
    print(f"  stream trades ({len(sm._trades)}):", flush=True)
    for t in list(sm._trades.values())[-5:]:
        print(f"    {t.figi} {t.direction} {t.quantity} @ {t.price:.2f}", flush=True)

async def wait_stream(sm, expect_figi, expect_side, expect_qty, seconds=60):
    for _ in range(seconds // 2):
        p = sm.get_position(expect_figi)
        if p and p.side == expect_side and p.qty == expect_qty:
            return True
        await asyncio.sleep(2)
    return False

async def main():
    from t_tech.invest import AsyncClient, OrderDirection, OrderType
    from app.bot.stream_manager import StreamManager

    sm = StreamManager(token=TOKEN, account_id=ACC, target="sandbox-invest-public-api.tbank.ru")
    ST["sm"] = sm
    sm_task = asyncio.create_task(sm._positions_loop())
    trades_task = asyncio.create_task(sm._trades_loop())
    await asyncio.sleep(8)
    log_state("StreamManager started")

    async with AsyncClient(TOKEN, target="sandbox-invest-public-api.tbank.ru") as c:
        # 1. Close ALRS SHORT 110 -> BUY 11 lots
        print("\n>>> Closing ALRS SHORT (BUY 11 lots)", flush=True)
        resp = await c.sandbox.post_sandbox_order(
            instrument_id=ALRS_UID, quantity=11,
            direction=OrderDirection.ORDER_DIRECTION_BUY,
            account_id=ACC, order_type=OrderType.ORDER_TYPE_MARKET,
            order_id=str(uuid4()),
        )
        print(f"  order_id={resp.order_id}", flush=True)
        await asyncio.sleep(5)
        log_state("after close ALRS")
        alrs_ok = sm.get_position(ALRS_FIGI) is None
        print(f"  ALRS closed in stream: {alrs_ok}", flush=True)

        # 2. Open SBER LONG 1 lot
        print("\n>>> Opening SBER LONG (BUY 1 lot)", flush=True)
        resp = await c.sandbox.post_sandbox_order(
            instrument_id=SBER_UID, quantity=1,
            direction=OrderDirection.ORDER_DIRECTION_BUY,
            account_id=ACC, order_type=OrderType.ORDER_TYPE_MARKET,
            order_id=str(uuid4()),
        )
        print(f"  order_id={resp.order_id}", flush=True)
        got = await wait_stream(sm, SBER_FIGI, "LONG", SBER_LOT)
        log_state("after open SBER")
        print(f"  SBER LONG seen in stream: {got}", flush=True)

        # 3. Close SBER LONG -> SELL 1 lot
        print("\n>>> Closing SBER LONG (SELL 1 lot)", flush=True)
        resp = await c.sandbox.post_sandbox_order(
            instrument_id=SBER_UID, quantity=1,
            direction=OrderDirection.ORDER_DIRECTION_SELL,
            account_id=ACC, order_type=OrderType.ORDER_TYPE_MARKET,
            order_id=str(uuid4()),
        )
        print(f"  order_id={resp.order_id}", flush=True)
        await asyncio.sleep(5)
        log_state("after close SBER")
        sber_ok = sm.get_position(SBER_FIGI) is None
        print(f"  SBER closed in stream: {sber_ok}", flush=True)

        # 4. Final portfolio
        pr = await c.operations.get_portfolio(account_id=ACC)
        print("\n=== final portfolio ===", flush=True)
        for p in pr.positions:
            if p.instrument_type == "currency":
                continue
            q = p.quantity.units + p.quantity.nano / 1e9
            print(f"  {p.figi} qty={q}", flush=True)

    sm_task.cancel()
    trades_task.cancel()

if __name__ == "__main__":
    asyncio.run(main())
