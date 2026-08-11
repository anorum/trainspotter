"""Regression: the materializer's two consumers must live in distinct groups.

A single Kafka group whose two members subscribe to different topics ends up
with partitions assigned to a member that ignores them; on the first Phase B
deploy this manifested as observations rows never landing while sessions did.
The fix is one group per topic. This test pins the group_id contract by
observing the kwargs the Materializer passes to AIOKafkaConsumer.
"""

from __future__ import annotations

import pytest
from api.materializer import Materializer
from blockade import bus
from blockade.config import Settings


class _FakeAIOKafkaConsumer:
    def __init__(self, *topics, **kwargs):
        self.topics = topics
        self.kwargs = kwargs


@pytest.fixture
def fake_kafka_consumers(monkeypatch: pytest.MonkeyPatch) -> list[_FakeAIOKafkaConsumer]:
    created: list[_FakeAIOKafkaConsumer] = []

    def factory(*topics, **kwargs) -> _FakeAIOKafkaConsumer:
        consumer = _FakeAIOKafkaConsumer(*topics, **kwargs)
        created.append(consumer)
        return consumer

    monkeypatch.setattr(bus, "AIOKafkaConsumer", factory)
    return created


def test_materializer_uses_a_distinct_consumer_group_per_topic(
    fake_kafka_consumers: list[_FakeAIOKafkaConsumer],
) -> None:
    settings = Settings(kafka_bootstrap="broker:9092")

    Materializer(settings, pool=object())

    assert len(fake_kafka_consumers) == 2, "one AIOKafkaConsumer per topic"
    by_topic = {c.topics[0]: c for c in fake_kafka_consumers}
    obs = by_topic[settings.kafka_observations_topic]
    sess = by_topic[settings.kafka_sessions_topic]

    assert obs.kwargs["group_id"] != sess.kwargs["group_id"], (
        "shared group causes Kafka to assign one topic's partitions to the "
        "member that ignores it - the exact Phase B deploy bug"
    )
    # The specific ids the deploy playbook and dashboards look for.
    assert obs.kwargs["group_id"] == "blockade-api-db-obs"
    assert sess.kwargs["group_id"] == "blockade-api-db-sess"
