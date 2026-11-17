"""Проверка следа зависания: цикл блокируется синхронно и не восстанавливается."""
import asyncio
import time

from app.services import loop_monitor


async def hold_forever() -> None:
    await asyncio.sleep(2.0)
    time.sleep(120.0)


async def main() -> None:
    loop_monitor.start_sampler()
    await hold_forever()


if __name__ == "__main__":
    asyncio.run(main())
