from scripts.ops.audit_account_history_preservation import compare_history


def snapshot(content='original', identity=None):
    return {'rows': {'1': {'content_hash': content, 'identity_hash': identity}}}


def test_unchanged_legacy_history_is_preserved():
    assert compare_history(snapshot(), snapshot())['preserved']


def test_deleted_changed_or_attributed_legacy_rows_are_detected():
    for after in ({'rows': {}}, snapshot('altered'), snapshot(identity='account')):
        assert not compare_history(snapshot(), after)['preserved']


def test_new_rows_do_not_hide_damage_to_old_rows():
    after = snapshot('altered')
    after['rows']['2'] = snapshot()['rows']['1']
    result = compare_history(snapshot(), after)
    assert result['changed_ids'] == ['1']
    assert result['added_ids'] == ['2']
    assert not result['preserved']
