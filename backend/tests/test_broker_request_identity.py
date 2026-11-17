import pytest
from app.bot.broker_request_identity import validate_broker_request_id


@pytest.mark.parametrize('value', [None, '', 'paper-924087c24fcd', 'a'*36, '12345678123456781234567812345678'])
def test_malformed_request_id_is_rejected(value):
    with pytest.raises(ValueError, match='broker_request_id_must_be_uuid'):
        validate_broker_request_id(value)


def test_canonical_uuid_is_preserved_for_idempotency():
    value = '4d7e08cb-3e38-44bd-9a94-f4a275c711cb'
    assert validate_broker_request_id(value) == value
