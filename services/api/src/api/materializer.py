"""Kafka to Postgres: the history store's feed.

One grouped consumer per topic (blockade-api-db-obs, blockade-api-db-sess)
over observations and sessions, batch upserts inside one transaction, Kafka
offsets committed only after the transaction commits - a crash replays a
batch and the idempotent upserts absorb it. Deterministic session ids and
the versioned observation key make at-least-once safe, the same argument as
everywhere else on the bus.

Fail-fast like the tailer: an unhandled error exits the process, kubelet
restarts, each group resumes from its committed offsets.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from blockade.bus import RecordConsumer
from blockade.config import Settings

from api import db

log = logging.getLogger(__name__)

FLUSH_RECORDS = 200
FLUSH_SECONDS = 10.0


class Materializer:
    """Owns the consumers and the flush loop."""

    def __init__(self, settings: Settings, pool) -> None:
        assert settings.kafka_bootstrap is not None
        self._pool = pool
        # One group per topic, deliberately: two members of a single group
        # subscribing to different topics confuses partition assignment, and
        # the observations partitions ended up owned by the member that
        # ignores them - zero observation rows ever landed. Separate groups
        # give each consumer full ownership of its own topic.
        self._obs = RecordConsumer(
            settings.kafka_bootstrap,
            settings.kafka_observations_topic,
            group_id="blockade-api-db-obs",
            client_id="blockade-api-db-obs",
        )
        self._sess = RecordConsumer(
            settings.kafka_bootstrap,
            settings.kafka_sessions_topic,
            group_id="blockade-api-db-sess",
            client_id="blockade-api-db-sess",
        )
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        await self._obs.start()
        await self._sess.start()
        self._tasks = [
            asyncio.create_task(self._run(self._obs, "observations")),
            asyncio.create_task(self._run(self._sess, "sessions")),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._obs.stop()
        await self._sess.stop()

    async def _run(self, consumer: RecordConsumer, kind: str) -> None:
        try:
            while True:
                batch = await consumer.get_batch(
                    timeout_ms=int(FLUSH_SECONDS * 1000), max_records=FLUSH_RECORDS
                )
                if not batch:
                    continue
                rows = []
                for record in batch:
                    if record.value is None:
                        continue
                    try:
                        rows.append(json.loads(record.value))
                    except ValueError:
                        log.error(
                            "unparseable %s record at %s:%s", kind, record.partition, record.offset
                        )
                if kind == "observations":
                    await db.upsert_batch(self._pool, rows, [])
                else:
                    await db.upsert_batch(self._pool, [], rows)
                # Offsets commit only after the transaction is durable.
                await consumer.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("materializer %s failed; exiting for a clean restart", kind)
            os._exit(1)
