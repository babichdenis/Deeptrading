from uuid import UUID

from app.bot.runtime import _new_order_id


def test_generated_order_ids_are_unique_canonical_broker_uuids():
    values = [_new_order_id() for _ in range(100)]
    assert len(set(values)) == 100
    assert all(str(UUID(value)) == value and UUID(value).version == 4 for value in values)
