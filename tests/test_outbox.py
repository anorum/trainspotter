"""The outbox publisher's delivery semantics.

Each test asserts one clause of the contract: every manifest record reaches the
topic at least once, in per-camera order, and a crash at any point in
publish-then-advance re-publishes rather than drops. The producer is a fake
that records sends and can be told to fail, because the semantics under test
are the outbox's, not aiokafka's.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from blockade.config import Settings
from blockade.schemas import CapturedAtSource, FetchStatus, FrameRecord
from poller.outbox import ManifestOutbox


class FakeProducer:
    """Records sends; optionally refuses acks to simulate a broker failure."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, bytes]] = []
        self.fail_acks = False
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def send(self, topic: str, key: str, value: bytes) -> asyncio.Future:
        self.sent.append((topic, key, value))
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        if self.fail_acks:
            fut.set_exception(ConnectionError("broker gone"))
        else:
            fut.set_result(None)
        return fut


def record(camera_id: str, minute: int) -> FrameRecord:
    captured = datetime(2026, 8, 10, 18, 0, tzinfo=UTC) + timedelta(minutes=minute)
    return FrameRecord(
        camera_id=camera_id,
        crossing_id="se-11th-12th",
        captured_at=captured,
        captured_at_source=CapturedAtSource.LAST_MODIFIED,
        fetched_at=captured,
        fetch_status=FetchStatus.OK,
        object_key=f"frames/{camera_id}/x/{minute}.jpg",
        poller_version="test",
    )


def write_manifest(root: Path, camera_id: str, hour: str, records: list[FrameRecord]) -> Path:
    path = root / camera_id / f"{hour}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        for r in records:
            fh.write(r.model_dump_json() + "\n")
    return path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        kafka_bootstrap="test:9092",
        manifest_dir=tmp_path / "manifests",
        outbox_dir=tmp_path / "outbox",
        odot_api_key=None,
    )


@pytest.fixture
def producer() -> FakeProducer:
    return FakeProducer()


@pytest.fixture
def outbox(settings: Settings, producer: FakeProducer) -> ManifestOutbox:
    return ManifestOutbox(settings, producer=producer)


async def test_publishes_every_line_keyed_and_in_order(
    settings: Settings, producer: FakeProducer, outbox: ManifestOutbox
) -> None:
    records = [record("odot-678", m) for m in range(5)]
    write_manifest(settings.manifest_dir, "odot-678", "2026-08-10-18", records)

    published = await outbox.drain_once()

    assert published == 5
    assert [key for _, key, _ in producer.sent] == ["odot-678"] * 5
    sent_times = [json.loads(v)["captured_at"] for _, _, v in producer.sent]
    assert sent_times == sorted(sent_times)
    assert all(topic == "crossing.frames.v1" for topic, _, _ in producer.sent)


async def test_wire_bytes_are_the_manifest_bytes(
    settings: Settings, producer: FakeProducer, outbox: ManifestOutbox
) -> None:
    """The topic carries exactly what the manifest holds - no re-serialization -
    so replay from either source is byte-identical."""
    frames = [record("odot-678", 0)]
    path = write_manifest(settings.manifest_dir, "odot-678", "2026-08-10-18", frames)

    await outbox.drain_once()

    assert producer.sent[0][2] == path.read_bytes().rstrip(b"\n")


async def test_a_second_drain_publishes_nothing_new(
    settings: Settings, producer: FakeProducer, outbox: ManifestOutbox
) -> None:
    write_manifest(settings.manifest_dir, "odot-678", "2026-08-10-18", [record("odot-678", 0)])
    await outbox.drain_once()

    assert await outbox.drain_once() == 0
    assert len(producer.sent) == 1


async def test_restart_resumes_from_the_position_file(
    settings: Settings, producer: FakeProducer
) -> None:
    write_manifest(settings.manifest_dir, "odot-678", "2026-08-10-18", [record("odot-678", 0)])
    await ManifestOutbox(settings, producer=producer).drain_once()

    write_manifest(settings.manifest_dir, "odot-678", "2026-08-10-18", [record("odot-678", 1)])
    fresh = ManifestOutbox(settings, producer=producer)  # new process, same disk
    assert await fresh.drain_once() == 1
    assert len(producer.sent) == 2


async def test_crash_before_position_advance_republishes(
    settings: Settings, producer: FakeProducer, outbox: ManifestOutbox
) -> None:
    """The at-least-once clause itself: acks fail, so the position must not
    move, and the next drain re-publishes. A dropped frame is unrecoverable;
    a duplicate is absorbed downstream by deterministic identity."""
    write_manifest(settings.manifest_dir, "odot-678", "2026-08-10-18", [record("odot-678", 0)])
    producer.fail_acks = True

    with pytest.raises(ConnectionError):
        await outbox.drain_once()

    producer.fail_acks = False
    assert await ManifestOutbox(settings, producer=producer).drain_once() == 1
    assert len(producer.sent) == 2  # the failed attempt and the successful one


async def test_an_unterminated_fragment_waits_for_its_newline(
    settings: Settings, producer: FakeProducer, outbox: ManifestOutbox
) -> None:
    """The reader can catch the writer mid-line. A fragment without its newline
    must stay unread - consuming it would publish a torn record and then skip
    the completed line's tail forever."""
    frames = [record("odot-678", 0)]
    path = write_manifest(settings.manifest_dir, "odot-678", "2026-08-10-18", frames)
    full_line = record("odot-678", 1).model_dump_json() + "\n"
    with path.open("a") as fh:
        fh.write(full_line[:40])  # torn mid-write

    assert await outbox.drain_once() == 1  # only the complete line

    with path.open("a") as fh:
        fh.write(full_line[40:])  # the writer finishes

    assert await outbox.drain_once() == 1
    assert json.loads(producer.sent[-1][2])["captured_at"].startswith("2026-08-10T18:01")


async def test_corrupt_line_is_skipped_and_the_rest_still_flows(
    settings: Settings, producer: FakeProducer, outbox: ManifestOutbox
) -> None:
    frames = [record("odot-678", 0)]
    path = write_manifest(settings.manifest_dir, "odot-678", "2026-08-10-18", frames)
    with path.open("a") as fh:
        fh.write('{"not": "a frame record"}\n')
    write_manifest(settings.manifest_dir, "odot-678", "2026-08-10-18", [record("odot-678", 2)])

    assert await outbox.drain_once() == 2
    assert len(producer.sent) == 2


async def test_rolls_across_hourly_files_in_order(
    settings: Settings, producer: FakeProducer, outbox: ManifestOutbox
) -> None:
    first_hour = [record("odot-678", m) for m in range(2)]
    write_manifest(settings.manifest_dir, "odot-678", "2026-08-10-18", first_hour)
    write_manifest(settings.manifest_dir, "odot-678", "2026-08-10-19", [record("odot-678", 60)])

    assert await outbox.drain_once() == 3
    sent_times = [json.loads(v)["captured_at"] for _, _, v in producer.sent]
    assert sent_times == sorted(sent_times)


async def test_cameras_are_independent(
    settings: Settings, producer: FakeProducer, outbox: ManifestOutbox
) -> None:
    write_manifest(settings.manifest_dir, "odot-678", "2026-08-10-18", [record("odot-678", 0)])
    write_manifest(settings.manifest_dir, "odot-681", "2026-08-10-18", [record("odot-681", 0)])

    assert await outbox.drain_once() == 2
    assert {key for _, key, _ in producer.sent} == {"odot-678", "odot-681"}


async def test_no_bootstrap_configured_means_no_publisher(tmp_path: Path) -> None:
    """The capture-only mode: Settings default is kafka_bootstrap=None, and the
    poller only constructs an outbox when it is set. This pins the default."""
    assert Settings(odot_api_key=None).kafka_bootstrap is None
