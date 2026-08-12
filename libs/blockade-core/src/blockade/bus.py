"""Kafka helpers shared by every service that touches the bus.

JSON on the wire, one pydantic model per topic (schemas.py stays the single
source of truth), and every message keyed - keys are an ordering contract, not
a routing detail: all records for one key land on one partition, which is the
only reason a consumer may assume it sees a camera's frames in order.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, ConsumerRecord, TopicPartition


class RecordProducer:
    """Thin wrapper over AIOKafkaProducer with the delivery semantics pinned.

    ``acks=all`` and idempotence are on from day one. On today's single broker
    they cost nothing extra, and when the cluster ever grows to RF=3 the
    semantics are already correct - flipping durability flags on a live
    pipeline is exactly the kind of change this avoids.
    """

    def __init__(self, bootstrap: str, client_id: str) -> None:
        self._producer = AIOKafkaProducer(
            bootstrap_servers=bootstrap,
            client_id=client_id,
            acks="all",
            enable_idempotence=True,
            compression_type="gzip",
            # Small deliberate latency so a backlog drain batches instead of
            # producing one request per record.
            linger_ms=50,
        )

    async def start(self) -> None:
        await self._producer.start()

    async def stop(self) -> None:
        """Flushes in-flight sends before closing."""
        await self._producer.stop()

    async def send(self, topic: str, key: str, value: bytes) -> asyncio.Future:
        """Queue one record. Returns a future that resolves on broker ack.

        The caller decides the ack barrier: send a batch, then await the
        futures together, then commit its own progress. That ordering is what
        makes the outbox at-least-once rather than at-most-once.
        """
        return await self._producer.send(topic, value=value, key=key.encode())

    @staticmethod
    async def await_acks(futures: Iterable[asyncio.Future]) -> None:
        await asyncio.gather(*futures)


class RecordConsumer:
    """Batch-at-a-time consumer with offsets committed by the caller.

    Auto-commit is off deliberately: it acknowledges records on a timer,
    which under a crash acknowledges work that never happened. The caller
    processes a batch, publishes its results, waits for those acks, and only
    then calls ``commit()`` - so the group's saved position never runs ahead
    of durable output. A crash replays a batch; deterministic downstream
    identity absorbs the duplicates. At-least-once, end to end.
    """

    def __init__(self, bootstrap: str, topic: str, group_id: str, client_id: str) -> None:
        self._consumer = AIOKafkaConsumer(
            topic,
            bootstrap_servers=bootstrap,
            group_id=group_id,
            client_id=client_id,
            enable_auto_commit=False,
            # A new group starts from the beginning of the log, not the end:
            # the first detector deployment should score the retained history,
            # and a group that already has committed offsets ignores this.
            auto_offset_reset="earliest",
        )

    async def start(self) -> None:
        await self._consumer.start()

    async def stop(self) -> None:
        await self._consumer.stop()

    async def get_batch(
        self, timeout_ms: int = 5000, max_records: int = 100
    ) -> list[ConsumerRecord]:
        """Up to ``max_records`` across assigned partitions, or whatever arrived
        within the timeout. An empty list is a quiet topic, not an error."""
        batches = await self._consumer.getmany(timeout_ms=timeout_ms, max_records=max_records)
        return [record for records in batches.values() for record in records]

    async def commit(self) -> None:
        await self._consumer.commit()


class TopicTailer:
    """Read a whole topic from the beginning and keep following it, groupless.

    For consumers whose state is a pure function of the log - the serving
    API rebuilds its world by replaying, every boot. Committed offsets would
    be dead weight (there is nothing to resume; the replay *is* the recovery),
    and a consumer group would split partitions across replicas when every
    replica needs the full log. So: no group - ``group_id=None`` subscription
    in the constructor lets aiokafka self-assign all partitions locally and
    start from the beginning via ``auto_offset_reset="earliest"``.

    ``caught_up`` reports whether the tail has passed the end offsets captured
    at start - the readiness signal that keeps a freshly booted server from
    answering with a half-rebuilt world.
    """

    def __init__(self, bootstrap: str, topic: str, client_id: str) -> None:
        self._bootstrap = bootstrap
        self._topic = topic
        self._client_id = client_id
        self._consumer: AIOKafkaConsumer | None = None
        self._boot_end_offsets: dict[TopicPartition, int] = {}
        self._positions: dict[TopicPartition, int] = {}

    async def start(self) -> None:
        # Constructed here rather than __init__: aiokafka requires a running
        # event loop at construction, and callers build this object from
        # synchronous app-wiring code.
        #
        # Subscription in the constructor with group_id=None is aiokafka's
        # native groupless mode: it self-assigns every partition of the topic
        # locally, no group coordination, and auto_offset_reset starts at the
        # beginning. Manual assign() via partitions_for_topic was tried first
        # and cannot work on a fresh consumer - topics() returns a metadata
        # copy without updating the cache that partitions_for_topic reads,
        # which was verified against the real broker after it killed a boot.
        self._consumer = AIOKafkaConsumer(
            self._topic,
            bootstrap_servers=self._bootstrap,
            client_id=self._client_id,
            group_id=None,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        await self._consumer.start()
        for _ in range(50):
            if self._consumer.assignment():
                break
            await asyncio.sleep(0.2)
        partitions = sorted(self._consumer.assignment(), key=lambda tp: tp.partition)
        if not partitions:
            raise RuntimeError(f"no partitions assigned for topic {self._topic}")
        self._boot_end_offsets = await self._consumer.end_offsets(partitions)
        # Seeded from the log start rather than from zero. A partition whose
        # records have all aged out of retention has log-start == log-end and
        # will never hand back a record to advance a zero seed, so ``caught_up``
        # would stay False forever - for the whole tailer, since it is an
        # ``all()``. One silent crossing must not hold the others hostage.
        self._positions = await self._consumer.beginning_offsets(partitions)

    async def stop(self) -> None:
        if self._consumer is not None:
            await self._consumer.stop()

    async def get_batch(
        self, timeout_ms: int = 1000, max_records: int = 500
    ) -> list[ConsumerRecord]:
        assert self._consumer is not None, "start() first"
        batches = await self._consumer.getmany(timeout_ms=timeout_ms, max_records=max_records)
        records = [record for chunk in batches.values() for record in chunk]
        for record in records:
            self._positions[TopicPartition(record.topic, record.partition)] = record.offset + 1
        return records

    @property
    def boot_end_offsets(self) -> dict[TopicPartition, int]:
        """The head of each partition as it stood at start.

        The line ``caught_up`` measures against, and the only description of
        "everything that already existed when this process began" any consumer
        of the tail can get.
        """
        return dict(self._boot_end_offsets)

    @property
    def caught_up(self) -> bool:
        """True once every partition has passed the end offset seen at start."""
        return all(self._positions.get(tp, 0) >= end for tp, end in self._boot_end_offsets.items())


class GroupProgress:
    """How far a replaying consumer has already published, kept as group offsets.

    A ``TopicTailer`` re-reads its whole topic every boot, which is how state
    rebuilds - but re-deriving history and re-announcing it are different acts.
    A service that produces downstream records from what it tails needs to know
    which of the records it is replaying it has already acted on, or every
    restart republishes the past over whatever has since corrected it: the
    backfill path re-derives history and deletes the sessions it supersedes,
    and a boot that republished them would put the superseded rows back.

    A consumer group's committed offset is exactly that bookkeeping, so this
    keeps one without joining the group. Partitions are handed in by the tailer
    that already self-assigned them, so there is no coordination, no rebalance,
    and no second copy of the partition-discovery problem ``TopicTailer``
    documents. The offset describes published output only; what to read is
    still the whole log.

    A group with no committed offsets starts at the boot end offsets. A first
    deployment inherits a topic whose history was already published by whatever
    ran before it, so none of that history is this process's to announce.
    """

    def __init__(self, bootstrap: str, group_id: str, client_id: str) -> None:
        self._bootstrap = bootstrap
        self._group_id = group_id
        self._client_id = client_id
        self._consumer: AIOKafkaConsumer | None = None
        self._boundary: dict[TopicPartition, int] = {}
        self._positions: dict[TopicPartition, int] = {}

    async def start(self, boot_end_offsets: Mapping[TopicPartition, int]) -> None:
        """Assign the tailer's partitions and read where the group left off."""
        self._consumer = AIOKafkaConsumer(
            bootstrap_servers=self._bootstrap,
            client_id=self._client_id,
            group_id=self._group_id,
            enable_auto_commit=False,
        )
        await self._consumer.start()
        # assign() rather than subscribe(): with a group_id this is aiokafka's
        # simple-consumer mode - offsets are stored and fetched, but the
        # consumer never joins the group, so it cannot be rebalanced away from
        # the partitions the tailer is actually reading.
        self._consumer.assign(list(boot_end_offsets))
        for tp, end in boot_end_offsets.items():
            committed = await self._consumer.committed(tp)
            self._boundary[tp] = end if committed is None else committed
        self._positions = dict(self._boundary)

    async def stop(self) -> None:
        if self._consumer is not None:
            await self._consumer.stop()

    def published(self, record: ConsumerRecord) -> bool:
        """True if an earlier life already emitted this record's consequences."""
        tp = TopicPartition(record.topic, record.partition)
        return record.offset < self._boundary.get(tp, 0)

    async def commit(self, records: Iterable[ConsumerRecord]) -> None:
        """Mark a batch as published. Callers await their producer acks first,
        so the committed offset never runs ahead of durable output - the same
        barrier ``RecordConsumer`` documents.

        Monotonic: replaying a record from below the boundary cannot walk the
        committed offset backwards.

        Records from a partition this bookkeeper was not given are skipped
        rather than committed. A groupless tailer picks up a partition added
        after boot on its next metadata refresh, but committing one that was
        never assigned is an error that would take the caller's loop down; the
        records publish anyway, and the next boot assigns the partition
        properly.
        """
        advanced: dict[TopicPartition, int] = {}
        for record in records:
            tp = TopicPartition(record.topic, record.partition)
            if tp not in self._boundary:
                continue
            position = record.offset + 1
            if position > self._positions.get(tp, 0):
                self._positions[tp] = position
                advanced[tp] = position
        if advanced and self._consumer is not None:
            await self._consumer.commit(advanced)
