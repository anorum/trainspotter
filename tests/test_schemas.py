"""Schema invariants, above all the stability of ``session_id``.

docs/history/design.md section 5 calls an unstable session ID expensive to
retrofit: every downstream consumer uses it for idempotency, so instability
means duplicate alerts and broken upserts on replay.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from blockade.schemas import BlockageSession, FetchStatus, FrameRecord

STARTED = datetime(2026, 8, 8, 14, 31, 0, tzinfo=UTC)


def test_session_id_is_deterministic():
    a = BlockageSession.make_session_id("SE_11TH_MILWAUKIE", STARTED)
    b = BlockageSession.make_session_id("SE_11TH_MILWAUKIE", STARTED)
    assert a == b


def test_session_id_is_timezone_independent():
    """The same instant expressed in a different offset must yield the same ID.
    Otherwise a replay on a host configured to a different timezone would mint
    new IDs for sessions that already exist."""
    utc = BlockageSession.make_session_id("SE_11TH_MILWAUKIE", STARTED)
    pacific = BlockageSession.make_session_id(
        "SE_11TH_MILWAUKIE", STARTED.astimezone(timezone(timedelta(hours=-7)))
    )
    assert utc == pacific


def test_session_id_rejects_naive_datetimes():
    with pytest.raises(ValueError, match="timezone-aware"):
        BlockageSession.make_session_id("SE_11TH_MILWAUKIE", STARTED.replace(tzinfo=None))


def test_session_id_differs_by_crossing_and_start():
    base = BlockageSession.make_session_id("SE_11TH_MILWAUKIE", STARTED)
    assert base != BlockageSession.make_session_id("SE_12TH_CLINTON", STARTED)
    assert base != BlockageSession.make_session_id(
        "SE_11TH_MILWAUKIE", STARTED + timedelta(minutes=1)
    )


def test_session_id_survives_reopen_and_close():
    """The ID is assigned at open and must not change when the session closes."""
    opened = BlockageSession(
        session_id=BlockageSession.make_session_id("SE_11TH_MILWAUKIE", STARTED),
        crossing_id="SE_11TH_MILWAUKIE",
        started_at=STARTED,
        is_open=True,
        detector_version="v0.1.0",
    )
    closed = opened.model_copy(
        update={
            "ended_at": STARTED + timedelta(minutes=22),
            "duration_seconds": 1320,
            "is_open": False,
        }
    )
    assert closed.session_id == opened.session_id


def test_frame_record_rejects_unknown_fields():
    """Strict schemas: silent field drift between the poller and its consumers
    would be discovered as missing data weeks later."""
    with pytest.raises(ValueError):
        FrameRecord(
            camera_id="odot-1234",
            crossing_id="SE_11TH_MILWAUKIE",
            captured_at=STARTED,
            captured_at_source="fetched_at",
            fetched_at=STARTED,
            poller_version="0.1.0",
            unexpected_field="boom",
        )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (FetchStatus.OK, False),
        (FetchStatus.DUPLICATE, True),
        (FetchStatus.NOT_MODIFIED, True),
        (FetchStatus.ERROR, False),
    ],
)
def test_is_duplicate_classification(status, expected):
    record = FrameRecord(
        camera_id="odot-1234",
        crossing_id="SE_11TH_MILWAUKIE",
        captured_at=STARTED,
        captured_at_source="fetched_at",
        fetched_at=STARTED,
        fetch_status=status,
        poller_version="0.1.0",
    )
    assert record.is_duplicate is expected
