from datetime import datetime

from app.bot.execution_reference import ExecutionReference
from app.bot.pending_dispatch import execution_lock


def record_reference_state(runtime, scope, figi, reason, reference=None):
    if getattr(runtime, '_intrabar_reference_scope', None) != scope:
        runtime._intrabar_reference_scope = scope
        runtime._intrabar_reference_status = {}
    states = runtime._intrabar_reference_status
    previous = states.get(figi, {})
    row = {'reason': reason, 'checked_at': runtime._bot_now().isoformat(),
           'market_at': reference.market_at.isoformat() if reference is not None and isinstance(reference.market_at, datetime) else None,
           'received_at': reference.received_at.isoformat() if reference is not None and isinstance(reference.received_at, datetime) else None}
    states[figi] = row
    events = getattr(runtime, 'events', None)
    if events is not None and previous.get('reason') != reason:
        events.log('PROTECTION_REFERENCE_STATE', figi=figi, **row)


async def check_intrabar_references(runtime):
    broker = runtime.broker
    scope = (broker.mode, broker._account)
    plans = dict(runtime._exit_plans)
    references = await broker.execution_references(list(plans))
    outcomes = {}
    async with execution_lock(runtime):
        for figi, plan in plans.items():
            if not runtime.running or runtime.broker is not broker or (broker.mode, broker._account) != scope:
                return outcomes
            if runtime._exit_plans.get(figi) is not plan:
                continue
            reference = references.get(figi)
            if not isinstance(reference, ExecutionReference):
                outcomes[figi] = 'missing_reference'
                record_reference_state(runtime, scope, figi, 'missing_reference')
                continue
            now = runtime._bot_now()
            try:
                reference.validate(figi=figi, decision_at=reference.market_at, as_of=now)
            except ValueError as error:
                outcomes[figi] = str(error)
                record_reference_state(runtime, scope, figi, str(error), reference)
                continue
            record_reference_state(runtime, scope, figi, 'reference_ready', reference)
            outcomes[figi] = await runtime._intrabar_handle(figi, reference.price)
    return outcomes
