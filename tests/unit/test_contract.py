from datetime import UTC, datetime

import pytest

from app.core.errors import AppError
from app.core.security import sign, verify_signature
from app.services.ingestion import parse_event
from app.services.processing import retry_delay


def test_signature_uses_exact_bytes():
    now = datetime(2026, 10, 16, tzinfo=UTC)
    ts, body = str(int(now.timestamp())), b'{"x": 1}'
    signature = sign("secret", ts, body)
    verify_signature(["secret"], ts, signature, body, now=now, tolerance=300)
    with pytest.raises(AppError, match="Signature"):
        verify_signature(["secret"], ts, signature, b'{"x":1}', now=now, tolerance=300)


@pytest.mark.parametrize("offset", [-301, 301])
def test_timestamp_window(offset):
    now = datetime(2026, 10, 16, tzinfo=UTC)
    ts = str(int(now.timestamp()) + offset)
    with pytest.raises(AppError) as exc:
        verify_signature(["secret"], ts, sign("secret", ts, b"{}"), b"{}", now=now, tolerance=300)
    assert exc.value.code == "stale_timestamp"


@pytest.mark.parametrize("body", [b'{"x":1,"x":2}', b'{"x":{"a":1,"a":2}}', b'{"x":NaN}', b"\xff"])
def test_ambiguous_or_invalid_json(body):
    with pytest.raises(AppError) as exc:
        parse_event(body)
    assert exc.value.code == "invalid_json"


def test_backoff_is_bounded_and_exponential(settings):
    assert retry_delay(1, settings, 0) == 2.5
    assert retry_delay(2, settings, 1) == 10
    assert retry_delay(20, settings, 1) == 300
