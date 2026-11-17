from types import SimpleNamespace

import pytest

from app.bot.runtime import PaperBotRuntime


@pytest.mark.parametrize("status", ["SUBMITTED", "APPROVED", "PENDING_APPROVAL", "PARTIAL_FILLED", "PENDING_RECONCILIATION", "FILLED", "UNKNOWN"])
def test_unresolved_entry_reserves_one_slot_even_if_already_held(status):
    runtime = PaperBotRuntime.__new__(PaperBotRuntime)
    runtime._held = {"A"}
    runtime.pending_orders = {
        "A": SimpleNamespace(action="open", status=status),
        "B": SimpleNamespace(action="open", status=status),
        "C": SimpleNamespace(action="close", status="SUBMITTED"),
    }
    assert runtime._occupied_entry_slots() == {"A", "B"}


@pytest.mark.parametrize("status", ["CANCELLED", "REJECTED"])
def test_terminal_unfilled_entry_releases_only_its_reservation(status):
    runtime = PaperBotRuntime.__new__(PaperBotRuntime)
    runtime._held = {"A"}
    runtime.pending_orders = {
        "A": SimpleNamespace(action="open", status=status),
        "B": SimpleNamespace(action="open", status=status),
    }
    assert runtime._occupied_entry_slots() == {"A"}
