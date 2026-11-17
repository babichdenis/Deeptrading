import asyncio
from datetime import timedelta

from app.bot.execution_reference import ExecutionReference


def execution_lock(runtime):
    if not hasattr(runtime, '_execution_lock'):
        runtime._execution_lock = asyncio.Lock()
    return runtime._execution_lock


def record_dispatch_state(runtime, figi, order, reason):
    order.meta = dict(getattr(order, 'meta', None) or {})
    if order.meta.get('dispatch_state') == reason:
        return
    order.meta['dispatch_state'] = reason
    order.meta['dispatch_state_at'] = runtime._bot_now().isoformat()
    runtime.events.log('ORDER_DISPATCH_STATE', figi=figi, order_id=order.id, reason=reason)


async def dispatch_pending(runtime):
    broker = runtime.broker
    scope = (broker.mode, broker._account)
    candidates = {figi: order for figi, order in runtime.pending_orders.items()
                  if order.status in ('SUBMITTED', 'APPROVED', 'PENDING_APPROVAL')
                  and (order.action != 'open' or not runtime.entries_paused)}
    if not candidates:
        return
    references = await broker.execution_references(list(candidates))
    async with execution_lock(runtime):
        if (not runtime.running or runtime.broker is not broker
                or (broker.mode, broker._account) != scope):
            return
        for figi, order in candidates.items():
            if runtime.pending_orders.get(figi) is not order:
                continue
            if order.status not in ('SUBMITTED', 'APPROVED', 'PENDING_APPROVAL'):
                continue
            if order.action == 'open' and runtime.entries_paused:
                continue
            reference = references.get(figi)
            if not isinstance(reference, ExecutionReference):
                record_dispatch_state(runtime, figi, order, 'waiting_market_reference')
                continue
            try:
                reference.validate(figi=figi, decision_at=order.created_at,
                                   as_of=runtime._bot_now(), max_age=timedelta(seconds=30))
            except ValueError as error:
                record_dispatch_state(runtime, figi, order, str(error))
                continue
            order.meta = dict(getattr(order, 'meta', None) or {})
            order.meta['execution_reference'] = {
                'price': reference.price,
                'market_at': reference.market_at.isoformat(),
                'received_at': reference.received_at.isoformat(),
                'decision_at': order.created_at.isoformat(),
            }
            record_dispatch_state(runtime, figi, order, 'reference_ready')
            await runtime._execute_pending(figi, reference)
