from uuid import UUID


def validate_broker_request_id(value):
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError('broker_request_id_must_be_uuid')
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise ValueError('broker_request_id_must_be_uuid') from error
    if str(parsed) != value.lower():
        raise ValueError('broker_request_id_must_be_uuid')
    return value
