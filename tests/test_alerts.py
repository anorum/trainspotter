"""Alert branch: one alert per train, not one per frame."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from blockade.alerts import AlertPolicy, RisingEdgeAlerter
from blockade.schemas import CrossingState, ObservationRecord

START = datetime(2026, 8, 9, 18, 56, tzinfo=UTC)


def obs(minutes: float, state=CrossingState.BLOCKED, crossing="SE_12TH_CLINTON"):
    return ObservationRecord(
        crossing_id=crossing,
        camera_id="odot-678",
        captured_at=START + timedelta(minutes=minutes),
        observed_at=START + timedelta(minutes=minutes),
        state=state,
        confidence=0.0 if state is CrossingState.UNKNOWN else 0.9,
        reason="mass spans 70 rows",
        object_key=f"frames/odot-678/{minutes}.jpg",
        detector_version="reference/test",
    )


def run(alerter, observations):
    return [a for o in observations if (a := alerter.observe(o)) is not None]


def test_one_alert_at_the_front_of_a_train():
    """A 55-minute blockage is one event, not twenty-five notifications."""
    alerts = run(RisingEdgeAlerter(), [obs(m) for m in range(0, 56, 2)])

    assert len(alerts) == 1
    assert alerts[0].started_at == START + timedelta(minutes=2)  # second confirmation


def test_a_single_stray_frame_does_not_alert():
    """One frame can be wrong. Two consecutive ones are much less likely to be,
    and the detector produces roughly fifteen spurious detections a day."""
    alerts = run(
        RisingEdgeAlerter(),
        [obs(0, CrossingState.CLEAR), obs(2), obs(4, CrossingState.CLEAR)],
    )

    assert alerts == []


def test_a_gap_between_railcars_does_not_re_alert():
    """The asymmetry, and the reason for it: a momentary clear reading mid-train
    -- a gap between cars, a low-profile well car -- must not re-arm and fire a
    second alert for the same train."""
    stream = [obs(m) for m in range(0, 10, 2)]
    stream += [obs(10, CrossingState.CLEAR)]  # one flicker
    stream += [obs(m) for m in range(12, 40, 2)]

    alerts = run(RisingEdgeAlerter(), stream)

    assert len(alerts) == 1


def test_a_genuine_clearing_re_arms_for_the_next_train():
    first = [obs(m) for m in range(0, 20, 2)]
    cleared = [obs(m, CrossingState.CLEAR) for m in range(20, 30, 2)]
    second = [obs(m) for m in range(60, 80, 2)]

    alerts = run(RisingEdgeAlerter(), first + cleared + second)

    assert len(alerts) == 2, "silence during, clean reset after, fresh alert next time"


def test_unknown_does_not_break_a_blocked_streak():
    """An unreadable frame mid-blockage is not a gap in the blockage."""
    stream = [obs(0), obs(2, CrossingState.UNKNOWN), obs(4)]

    alerts = run(RisingEdgeAlerter(), stream)

    assert len(alerts) == 1


def test_unknown_alone_never_alerts():
    assert run(RisingEdgeAlerter(), [obs(m, CrossingState.UNKNOWN) for m in range(0, 20, 2)]) == []


def test_a_long_silence_re_arms():
    """If a camera dies mid-blockage and never reports CLEAR, the crossing must
    not stay armed forever and mute the next real train."""
    alerter = RisingEdgeAlerter(AlertPolicy(reset_after=timedelta(minutes=20)))
    run(alerter, [obs(0), obs(2)])
    assert alerter.is_alerted("SE_12TH_CLINTON")

    alerts = run(alerter, [obs(60), obs(62)])  # nothing heard for an hour

    assert len(alerts) == 1


def test_crossings_alert_independently():
    alerter = RisingEdgeAlerter()
    stream = []
    for m in range(0, 10, 2):
        stream += [obs(m, crossing="SE_12TH_CLINTON"), obs(m, crossing="SE_11TH_MILWAUKIE")]

    alerts = run(alerter, stream)

    assert {a.crossing_id for a in alerts} == {"SE_12TH_CLINTON", "SE_11TH_MILWAUKIE"}
    assert len(alerts) == 2


def test_clearing_needs_more_confirmation_than_alerting():
    """Slow to clear, quick to fire -- the asymmetry stated as a property."""
    policy = AlertPolicy()

    assert policy.clear_confirmations > policy.confirmations


def test_a_stream_of_unknowns_is_silence_not_hearing():
    """UNKNOWN is absence of evidence, so it must not hold off the re-arm.
    A camera gone glare-blind mid-blockage - or a non-scoring camera's
    permanent UNKNOWN heartbeat - would otherwise keep last_seen fresh
    forever, and the next real train after an outage would pass unannounced."""
    alerter = RisingEdgeAlerter()

    first = run(alerter, [obs(0), obs(2)])
    assert len(first) == 1, "the first train alerts"

    # 30 minutes of UNKNOWN heartbeat: longer than reset_after (20m), and the
    # only traffic on the crossing. It must count as silence.
    heartbeat = [obs(4 + m, CrossingState.UNKNOWN) for m in range(0, 30, 2)]
    assert run(alerter, heartbeat) == []

    second = run(alerter, [obs(36), obs(38)])
    assert len(second) == 1, "the outage re-armed the crossing; the next train alerts"
