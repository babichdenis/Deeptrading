_MISSING_ACCOUNT = "__missing_account__"


def _active_route_mode():
    from app.bot.runtime import runtime
    mode = getattr(runtime, 'broker_mode', None)
    if mode in ('sandbox', 'live'):
        return mode
    from app.config import get_settings
    mode = get_settings().bot_mode
    return mode if mode in ('sandbox', 'live') else 'sandbox'


def _account_scope_for_routes() -> str | None:
    from app.config import get_settings
    account = get_settings().get_account(_active_route_mode())
    return str(account) if account else _MISSING_ACCOUNT
