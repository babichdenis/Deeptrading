from app.bot.universe.runtime_select import constrain_manual_candidates


def test_manual_order_preserved_but_cannot_override_broker_identity_or_lot():
    candidates = [dict(figi='A', ticker='ACTUAL', lot=1000, lot_size=1000, short_allowed=True),
                  dict(figi='B', ticker='B', lot=10, short_allowed=True)]
    decisions = [dict(figi=f, ticker=f, reasons=[]) for f in ('A', 'B')]
    requested = [dict(figi='A', ticker='WRONG', lot=1, lot_size=1, side='short'),
                 dict(figi='A', side='long')]
    selected, audited = constrain_manual_candidates(candidates, decisions, requested)
    assert len(selected) == 1
    assert selected[0]['ticker'] == 'ACTUAL'
    assert selected[0]['lot'] == selected[0]['lot_size'] == 1000
    assert selected[0]['side'] == 'short'
    assert audited[1]['reasons'] == ['outside_manual_selection']
    assert decisions[1]['reasons'] == []


def test_manual_choice_cannot_bypass_data_or_direction_rejection():
    candidates = [dict(figi='A', short_allowed=False)]
    decisions = [dict(figi='A', ticker='A', reasons=[]),
                 dict(figi='B', ticker='B', reasons=['insufficient_valid_history'])]
    requested = [dict(figi='A', side='short'), dict(figi='B'), dict(figi='UNKNOWN')]
    selected, audited = constrain_manual_candidates(candidates, decisions, requested)
    assert not selected
    assert [row['reasons'] for row in audited] == [
        ['short_enabled_not_confirmed'], ['insufficient_valid_history'], ['manual_candidate_unavailable']]
