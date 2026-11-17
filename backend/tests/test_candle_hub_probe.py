from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
from unittest.mock import Mock

import pytest
from app.services.candle_hub_probe import probe_source_window


@dataclass
class Response:
    candles:list


START=datetime(2026,11,9,12,tzinfo=timezone.utc)


def test_successful_empty_response_does_not_claim_no_trades():
    result=probe_source_window('F',START,START+timedelta(minutes=5),lambda *args:Response([]))
    assert result['status']=='response_received'
    assert result['absence_proven'] is False
    assert len(result['response_sha256'])==64
    assert result['writes_database'] is False


def test_error_is_distinct_from_empty_and_does_not_expose_exception_text():
    fetch=Mock(side_effect=RuntimeError('PRIVATE'))
    result=probe_source_window('F',START,START+timedelta(minutes=5),fetch)
    assert result['status']=='source_error'
    assert 'PRIVATE' not in str(result)


def test_oversized_request_rejected_before_source_call():
    fetch=Mock()
    with pytest.raises(ValueError):probe_source_window('F',START,START+timedelta(hours=2),fetch)
    fetch.assert_not_called()
