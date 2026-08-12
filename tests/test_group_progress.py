"""The published-offset boundary that keeps a full replay from republishing.

A ``TopicTailer`` re-reads its whole topic every boot, which is how state
rebuilds. Without a boundary a service that produces from that tail would also
re-emit everything it re-derives, putting back exactly the rows the batch path
deleted when it superseded them. ``GroupProgress`` is the boundary: a consumer
group's committed offset, kept without ever joining the group.

These pin what it lets through, against a fake broker.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from aiokafka import TopicPartition
from blockade import bus
from blockade.bus import GroupProgress

TOPIC = "crossing.observations.v1"
PARTITION = TopicPartition(TOPIC, 0)


@dataclass
class FakeRecord:
    offset: int
    topic: str = TOPIC
    partition: int = 0


class FakeOffsetConsumer:
    """Stores committed offsets and records what gets committed back."""

    def __init__(self, committed: int | None = None, **kwargs) -> None:
        self._committed = committed
        self.kwargs = kwargs
        self.assigned: list[TopicPartition] = []
        self.commits: list[dict[TopicPartition, int]] = []

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def assign(self, partitions: list[TopicPartition]) -> None:
        self.assigned = list(partitions)

    async def committed(self, partition: TopicPartition) -> int | None:  # noqa: ARG002
        return self._committed

    async def commit(self, offsets: dict[TopicPartition, int]) -> None:
        self.commits.append(dict(offsets))


async def bookkeeper(
    monkeypatch: pytest.MonkeyPatch, *, head: int, committed: int | None = None
) -> tuple[GroupProgress, FakeOffsetConsumer]:
    created: list[FakeOffsetConsumer] = []

    def factory(**kwargs) -> FakeOffsetConsumer:
        consumer = FakeOffsetConsumer(committed, **kwargs)
        created.append(consumer)
        return consumer

    monkeypatch.setattr(bus, "AIOKafkaConsumer", factory)
    progress = GroupProgress("broker:9092", group_id="blockade-test", client_id="test")
    await progress.start({PARTITION: head})
    return progress, created[0]


async def test_the_committed_offset_is_the_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Everything a previous life acknowledged is already downstream; only what
    it never reached is this life's to publish."""
    progress, _ = await bookkeeper(monkeypatch, head=10, committed=4)

    assert [progress.published(FakeRecord(offset)) for offset in range(6)] == [
        True,
        True,
        True,
        True,
        False,
        False,
    ]


async def test_a_group_with_no_commits_starts_at_the_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first deployment inherits a topic whose history someone else already
    published, so none of it is this process's to announce - only what arrives
    after it boots."""
    progress, _ = await bookkeeper(monkeypatch, head=7)

    assert progress.published(FakeRecord(6))
    assert not progress.published(FakeRecord(7))


async def test_commits_advance_the_boundary_and_never_walk_it_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay hands back records from below the boundary on every boot. If those
    moved the committed offset, a crash mid-replay would leave the group parked
    in the distant past and the next boot would republish everything since."""
    progress, consumer = await bookkeeper(monkeypatch, head=10, committed=4)

    await progress.commit([FakeRecord(4), FakeRecord(5)])
    assert consumer.commits == [{PARTITION: 6}]

    await progress.commit([FakeRecord(0), FakeRecord(1)])
    assert consumer.commits == [{PARTITION: 6}], "a replayed record commits nothing"

    await progress.commit([FakeRecord(6)])
    assert consumer.commits[-1] == {PARTITION: 7}


async def test_a_partition_added_after_boot_is_published_but_not_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tailer is groupless and self-assigns whatever the topic grows to, so
    it can hand back a partition this bookkeeper was never assigned. Committing
    one of those raises IllegalStateError on the real consumer and would take
    the caller's loop down with it. The record still publishes - an unknown
    partition has no boundary behind it - and the next boot assigns it."""
    progress, consumer = await bookkeeper(monkeypatch, head=10, committed=4)
    grown = FakeRecord(offset=0, partition=7)

    assert not progress.published(grown)
    await progress.commit([grown, FakeRecord(5)])

    assert consumer.commits == [{PARTITION: 6}]


async def test_the_bookkeeper_takes_the_tailers_partitions_without_joining_the_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual assignment, deliberately: a group member could be rebalanced off
    the partitions the tailer is actually reading, and partition discovery on a
    fresh consumer is the trap `TopicTailer` documents. The tailer already
    self-assigned them, so they are handed over rather than rediscovered."""
    _, consumer = await bookkeeper(monkeypatch, head=3)

    assert consumer.assigned == [PARTITION]
    assert consumer.kwargs["group_id"] == "blockade-test"
    assert consumer.kwargs["enable_auto_commit"] is False
