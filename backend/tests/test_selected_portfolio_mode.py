from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from app.api.routes import sandbox, _account_scope
from app.bot.runtime import runtime

@pytest.mark.parametrize("mode", ["sandbox", "live"])
def test_portfolio_uses_selected_runtime_mode(monkeypatch, mode):
    settings = SimpleNamespace(bot_mode="live", get_account=lambda m: m+"_ACCOUNT", get_token=lambda m: m+"_TOKEN", get_target=lambda m: m+"_TARGET")
    monkeypatch.setattr(runtime, "broker_mode", mode)
    monkeypatch.setattr(sandbox, "get_settings", lambda: settings)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    assert sandbox._active_mode() == mode
    assert sandbox._active_creds() == (mode+"_TOKEN", mode+"_TARGET", mode+"_ACCOUNT")
    assert _account_scope._account_scope_for_routes() == mode+"_ACCOUNT"

def test_missing_live_account_never_falls_back(monkeypatch):
    monkeypatch.setattr(runtime, "broker_mode", "live")
    monkeypatch.setattr(sandbox, "get_settings", lambda: SimpleNamespace(get_account=lambda m: "SANDBOX" if m=="sandbox" else None, get_token=lambda m:"token", get_target=lambda m:"target"))
    with pytest.raises(RuntimeError): sandbox._active_creds()

def test_portfolio_cache_changes_with_account(monkeypatch):
    monkeypatch.setattr(sandbox, "_active_mode", lambda:"sandbox")
    monkeypatch.setattr(sandbox, "_account_scope_for_routes", lambda:"A")
    monkeypatch.setattr(sandbox, "_portfolio_cache", None)
    fetch=Mock(side_effect=["first", "second"])
    monkeypatch.setattr(sandbox, "_get_portfolio", fetch)
    assert sandbox._get_portfolio_cached()=="first"
    monkeypatch.setattr(sandbox, "_account_scope_for_routes", lambda:"B")
    assert sandbox._get_portfolio_cached()=="second"
    assert fetch.call_count==2
