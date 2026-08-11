"""TopicTailer's startup metadata handling.

The regression this pins: partitions_for_topic reads cached metadata, and a
freshly started consumer has cached nothing - it returns None, the assignment
was empty, and seek_to_beginning asserted. The first in-cluster boot of the
serving API died exactly this way. start() must force a metadata fetch and
retry before giving up, and must fail loudly rather than assign nothing.
"""

from __future__ import annotations

import pytest
from aiokafka import TopicPartition
from blockade import bus
from blockade.bus import TopicTailer


class FakeConsumer:
    """Metadata appears only after topics() is called, as on a real broker."""

    def __init__(self, *args, partitions: set[int] | None = {0, 1, 2}, **kwargs):
        self._partitions = partitions
        self._metadata_fetched = False
        self.assigned: list[TopicPartition] | None = None
        self.sought = False

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def topics(self) -> set[str]:
        self._metadata_fetched = True
        return {"crossing.sessions.v1"}

    def partitions_for_topic(self, topic: str) -> set[int] | None:
        if not self._metadata_fetched:
            return None
        return self._partitions

    def assign(self, partitions: list[TopicPartition]) -> None:
        self.assigned = partitions

    async def seek_to_beginning(self, *partitions: TopicPartition) -> None:
        assert partitions, "No partitions are currently assigned"
        self.sought = True

    async def end_offsets(self, partitions: list[TopicPartition]) -> dict:
        return {tp: 5 for tp in partitions}


@pytest.fixture
def fake_consumer_cls(monkeypatch: pytest.MonkeyPatch):
    created: list[FakeConsumer] = []

    def factory(*args, **kwargs):
        consumer = FakeConsumer()
        created.append(consumer)
        return consumer

    monkeypatch.setattr(bus, "AIOKafkaConsumer", factory)
    return created


async def test_start_fetches_metadata_before_assigning(fake_consumer_cls) -> None:
    tailer = TopicTailer("broker:9092", "crossing.sessions.v1", "test")

    await tailer.start()

    consumer = fake_consumer_cls[0]
    assert consumer.assigned == [
        TopicPartition("crossing.sessions.v1", p) for p in (0, 1, 2)
    ]
    assert consumer.sought, "seek must happen after a non-empty assignment"
    assert not tailer.caught_up, "boot end offsets captured; nothing consumed yet"


async def test_start_fails_loudly_when_the_topic_never_appears(
    monkeypatch: pytest.MonkeyPatch, fake_consumer_cls
) -> None:
    monkeypatch.setattr(
        FakeConsumer, "partitions_for_topic", lambda self, topic: None
    )
    # No sleeping through ten real seconds in a unit test.
    async def no_sleep(_: float) -> None: ...
    monkeypatch.setattr(bus.asyncio, "sleep", no_sleep)

    tailer = TopicTailer("broker:9092", "missing.topic", "test")

    with pytest.raises(RuntimeError, match="no partitions visible"):
        await tailer.start()
