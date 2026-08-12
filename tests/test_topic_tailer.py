"""TopicTailer's startup contract, pinned against a fake consumer.

History that shaped this: the first in-cluster boot died because manual
assign() via partitions_for_topic cannot work on a fresh consumer (topics()
returns a metadata copy without updating the cache partitions_for_topic
reads - verified against the real broker). The tailer now subscribes in the
constructor with group_id=None, aiokafka's native groupless mode, and waits
for the self-assignment to appear. These tests pin that wait and the loud
failure when assignment never arrives.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from aiokafka import TopicPartition
from blockade import bus
from blockade.bus import TopicTailer


@dataclass
class FakeRecord:
    topic: str
    partition: int
    offset: int
    value: bytes = b"{}"


class FakeConsumer:
    """Assignment appears one poll after start, as with a real broker."""

    log_start: dict[int, int] = {}  # noqa: RUF012
    """Per-partition first retained offset. Equal to the end offset means every
    record on that partition has aged out of retention."""

    def __init__(self, topic: str | None = None, **kwargs):
        self.topic = topic
        self.kwargs = kwargs
        self._assignment: set[TopicPartition] = set()
        self._started = False
        self.pending: list[FakeRecord] = []

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None: ...

    def assignment(self) -> set[TopicPartition]:
        if self._started and self.topic is not None:
            # Simulates metadata resolving shortly after start.
            self._assignment = {TopicPartition(self.topic, p) for p in (0, 1, 2)}
        return self._assignment

    async def end_offsets(self, partitions: list[TopicPartition]) -> dict:
        return {tp: 5 for tp in partitions}

    async def beginning_offsets(self, partitions: list[TopicPartition]) -> dict:
        return {tp: self.log_start.get(tp.partition, 0) for tp in partitions}

    async def getmany(self, timeout_ms: int = 0, max_records: int = 0) -> dict:  # noqa: ARG002
        batch, self.pending = self.pending, []
        out: dict[TopicPartition, list[FakeRecord]] = {}
        for record in batch:
            out.setdefault(TopicPartition(record.topic, record.partition), []).append(record)
        return out


@pytest.fixture
def fake_consumers(monkeypatch: pytest.MonkeyPatch) -> list[FakeConsumer]:
    created: list[FakeConsumer] = []

    def factory(topic: str | None = None, **kwargs) -> FakeConsumer:
        consumer = FakeConsumer(topic, **kwargs)
        created.append(consumer)
        return consumer

    monkeypatch.setattr(bus, "AIOKafkaConsumer", factory)
    return created


async def test_start_subscribes_groupless_and_waits_for_assignment(
    fake_consumers: list[FakeConsumer],
) -> None:
    tailer = TopicTailer("broker:9092", "crossing.sessions.v1", "test")

    await tailer.start()

    consumer = fake_consumers[0]
    assert consumer.topic == "crossing.sessions.v1", "subscription via constructor"
    assert consumer.kwargs["group_id"] is None
    assert consumer.kwargs["auto_offset_reset"] == "earliest"
    assert not tailer.caught_up, "boot end offsets captured; nothing consumed yet"


async def test_start_fails_loudly_when_assignment_never_arrives(
    monkeypatch: pytest.MonkeyPatch, fake_consumers: list[FakeConsumer]
) -> None:
    monkeypatch.setattr(FakeConsumer, "assignment", lambda self: set())

    async def no_sleep(_: float) -> None: ...

    monkeypatch.setattr(bus.asyncio, "sleep", no_sleep)

    tailer = TopicTailer("broker:9092", "missing.topic", "test")

    with pytest.raises(RuntimeError, match="no partitions assigned"):
        await tailer.start()


async def test_a_partition_whose_log_expired_does_not_hold_caught_up_down(
    monkeypatch: pytest.MonkeyPatch, fake_consumers: list[FakeConsumer]
) -> None:
    """A crossing that stops reporting for longer than retention leaves its
    partition with log-start == log-end. Nothing will ever be fetched from it,
    so a position seeded at zero could never reach the head - and because
    `caught_up` is an `all()`, that one silent partition would hold the flag
    False for the whole topic. The sessionizer gates its gap-deadline sweep on
    that flag, so stuck-false means no session ever closes again."""
    monkeypatch.setattr(FakeConsumer, "log_start", {2: 5})
    topic = "crossing.observations.v1"
    tailer = TopicTailer("broker:9092", topic, "test")
    await tailer.start()
    consumer = fake_consumers[0]

    assert not tailer.caught_up, "partitions 0 and 1 still have five records each"

    consumer.pending = [
        FakeRecord(topic, partition, offset) for partition in (0, 1) for offset in range(5)
    ]
    await tailer.get_batch()

    assert tailer.caught_up, "the expired partition has nothing left to deliver"
