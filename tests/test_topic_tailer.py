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

import pytest
from aiokafka import TopicPartition
from blockade import bus
from blockade.bus import TopicTailer


class FakeConsumer:
    """Assignment appears one poll after start, as with a real broker."""

    def __init__(self, topic: str | None = None, **kwargs):
        self.topic = topic
        self.kwargs = kwargs
        self._assignment: set[TopicPartition] = set()
        self._started = False

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
