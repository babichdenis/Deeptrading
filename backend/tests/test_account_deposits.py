from types import SimpleNamespace

from app.api.routes import sandbox as s


def test_account_caption_masks_long_id(monkeypatch):
    monkeypatch.setattr(s, "_active_creds", lambda: ("t", "target", "12345678901234567890"))
    assert s._account_caption() == "…7890"


def test_account_caption_keeps_short_id(monkeypatch):
    monkeypatch.setattr(s, "_active_creds", lambda: ("t", "target", "123"))
    assert s._account_caption() == "123"


def test_account_caption_empty(monkeypatch):
    monkeypatch.setattr(s, "_active_creds", lambda: ("t", "target", ""))
    assert s._account_caption() == "—"


def test_cash_deposits_sums_input_output(monkeypatch):
    monkeypatch.setattr(s, "_active_mode", lambda: "live")
    monkeypatch.setattr(s, "_active_creds", lambda: ("t", "target", "acc"))
    monkeypatch.setattr(s, "_deposits_cache", None)
    ops = SimpleNamespace(operations=[
        SimpleNamespace(type="Пополнение счёта", payment=1000.0,
                        date="2026-11-15T21:29:04Z", currency="RUB"),
        SimpleNamespace(type="Пополнение счёта", payment=20000.0,
                        date="2026-11-15T21:34:57Z", currency="RUB"),
        SimpleNamespace(type="Вывод средств", payment=500.0,
                        date="2026-11-15T22:00:00Z", currency="RUB"),
        SimpleNamespace(type="Покупка", payment=-999.0,
                        date="2026-11-15T22:01:00Z", currency="RUB"),
        SimpleNamespace(type="Комиссия", payment=-5.0,
                        date="2026-11-15T22:02:00Z", currency="RUB"),
    ])
    monkeypatch.setattr(s, "_get_operations", lambda from_days=3: ops)
    d = s._cash_deposits()
    assert d["net"] == 20500.0  # 1000 + 20000 - 500 (сделки/комиссии не в счёт)
    assert len(d["operations"]) == 3
    assert [e["type"] for e in d["operations"]] == ["INPUT", "INPUT", "OUTPUT"]
    assert d["operations"][0]["amount"] == 1000.0
    assert d["operations"][2]["amount"] == 500.0
    assert d["last_sync"]


def test_cash_deposits_empty_on_no_ops(monkeypatch):
    monkeypatch.setattr(s, "_active_mode", lambda: "sandbox")
    monkeypatch.setattr(s, "_active_creds", lambda: ("t", "target", "acc"))
    monkeypatch.setattr(s, "_deposits_cache", None)
    monkeypatch.setattr(s, "_get_operations", lambda from_days=3: SimpleNamespace(operations=[]))
    d = s._cash_deposits()
    assert d["net"] == 0.0
    assert d["operations"] == []
