"""Kafka helpers shared by every service that touches the bus.

JSON on the wire, one pydantic model per topic (schemas.py stays the single
source of truth), and every message keyed - keys are an ordering contract, not
a routing detail: all records for one key land on one partition, which is the
only reason a consumer may assume it sees a camera's frames in order.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, ConsumerRecord


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
